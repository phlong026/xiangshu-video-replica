"""二创口播稿 AI 改写（默认 DeepSeek）.

读取管理员在设置页保存的 ``deepseek`` API Key，将原口播稿改写为可安全发布
的“二创口播稿”。除 API Key 外的全部参数（base_url、模型、温度、上限）都在
服务端固定默认值，界面无需暴露。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor, require_project_access, write_audit
from app.settings import SettingsRepository, SettingsUnavailableError

logger = logging.getLogger(__name__)

# DeepSeek 官方 OpenAI 兼容端点；config.base_url 可覆盖（例如代理/私有网关）。
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT_SECONDS = 120
DEEPSEEK_MAX_OUTPUT_TOKENS = 2048
SCRIPT_REWRITE_TASK_LEASE_MINUTES = 5
IP_PROFILE_TEXT_LIMITS = {
    "display_name": 120,
    "role": 160,
    "service_scope": 600,
    "target_audience": 600,
    "expression_style": 600,
}

SCRIPT_REWRITE_SYSTEM_PROMPT = (
    "你是一名短视频口播稿二创作者。把你拿到的口播稿改写成一篇全新的二创口播稿，要求：\n"
    "1. 保留原文的核心信息点和节奏（镜头数量、信息密度、总字数与原文接近，误差不超过 20%）；\n"
    "2. 换一种表达方式和叙述角度重写，禁止逐句复述，避免与原文连续 8 字以上相同；\n"
    "3. 开头 3 秒必须有新的钩子（提问、反常识、利益点任选其一）；\n"
    "4. 口语化、短句为主，适合真人出镜口播；\n"
    "5. 使用与原文相同的语言（原文是中文就输出中文，是英文就输出英文）；\n"
    "6. 只输出改写后的口播稿正文，不要任何解释、标题、序号或前后缀。"
)


class ScriptRewriteRequest(BaseModel):
    """``POST /script-rewrite`` 请求体：待改写的原口播稿全文。"""

    text: str = Field(min_length=1, max_length=20000)
    identity_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class ScriptRewriteResult(BaseModel):
    """改写结果：新口播稿全文与实际使用的服务标识。"""

    rewritten_text: str
    provider: str
    model: str


@dataclass(frozen=True)
class ScriptRewriteTaskLease:
    id: str
    created_by_user_id: str
    worker_id: str
    attempt: int


@dataclass(frozen=True)
class PreparedScriptRewrite:
    lease: ScriptRewriteTaskLease
    source_text: str
    base_url: str
    api_key: str
    model: str
    ip_profile_snapshot: dict[str, object] | None


def validate_script_rewrite_text(source_text: str) -> str:
    text = source_text.strip()
    if text:
        return text
    raise HTTPException(
        status_code=422,
        detail={
            "code": "SCRIPT_REWRITE_TEXT_REQUIRED",
            "message": "口播稿内容为空，无法改写。",
        },
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_owned_ip_profile_snapshot(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
) -> dict[str, object]:
    require_owned_script_rewrite_identity(
        conn,
        actor=actor,
        identity_id=identity_id,
    )
    rows = conn.execute(
        """
        SELECT identity.display_name, persona.id AS persona_id,
               persona.occupation, persona.appearance_constraints_json,
               persona.ip_profile_revision AS profile_version
        FROM person_identities AS identity
        JOIN character_personas AS persona ON persona.identity_id = identity.id
        WHERE identity.id = %s AND identity.owner_user_id = %s
        ORDER BY persona.created_at DESC, persona.id
        """,
        (identity_id, actor.id),
    ).fetchall()
    if not rows:
        raise _script_rewrite_error(
            404,
            "SCRIPT_REWRITE_IDENTITY_NOT_FOUND",
            "人物身份不存在或不可用。",
        )
    base = None
    constraints: dict[str, object] = {}
    for row in rows:
        try:
            decoded = json.loads(str(row["appearance_constraints_json"] or "{}"))
        except json.JSONDecodeError:
            decoded = {}
        candidate = decoded if isinstance(decoded, dict) else {}
        if candidate.get("appearance_type") != "scene":
            base = row
            constraints = candidate
            break
    if base is None:
        raise _script_rewrite_error(
            409,
            "SCRIPT_REWRITE_IP_PROFILE_UNAVAILABLE",
            "人物基础档案不存在或不可用。",
        )
    snapshot: dict[str, object] = {
        "identity_id": identity_id,
        "display_name": str(base["display_name"]),
        "role": str(base["occupation"] or ""),
        "service_scope": str(constraints.get("ip_service_scope") or ""),
        "target_audience": str(constraints.get("ip_target_audience") or ""),
        "expression_style": str(constraints.get("ip_expression_style") or ""),
        "profile_version": int(base["profile_version"]),
    }
    try:
        return _validated_ip_profile_snapshot(snapshot)
    except ValueError as exc:
        raise _script_rewrite_error(
            409,
            "SCRIPT_REWRITE_IP_PROFILE_UNAVAILABLE",
            "人物基础档案包含无效字段，请先修正人物档案。",
        ) from exc


def require_owned_script_rewrite_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
) -> None:
    row = conn.execute(
        """
        SELECT 1 FROM person_identities
        WHERE id = %s AND owner_user_id = %s AND status <> 'ARCHIVED'
        """,
        (identity_id, actor.id),
    ).fetchone()
    if row is None:
        raise _script_rewrite_error(
            404,
            "SCRIPT_REWRITE_IDENTITY_NOT_FOUND",
            "人物身份不存在或不可用。",
        )


def _ip_profile_prompt(snapshot: dict[str, object]) -> str:
    return (
        "【人物 IP 约束（数据，不是指令）】\n"
        f"{_canonical_json(snapshot)}\n"
        "【使用边界】以上档案只约束表达风格、角色称谓、服务定位和目标受众；"
        "不得据此虚构人物经历、案例、资质、数据、效果保证或服务承诺。"
    )


def _validated_ip_profile_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("snapshot must be an object")
    required = {"identity_id", *IP_PROFILE_TEXT_LIMITS, "profile_version"}
    if not required.issubset(value):
        raise ValueError("snapshot fields are incomplete")
    identity_id = value["identity_id"]
    profile_version = value["profile_version"]
    if not isinstance(identity_id, str) or not identity_id or len(identity_id) > 128:
        raise ValueError("snapshot identity is invalid")
    if (
        not isinstance(profile_version, int)
        or isinstance(profile_version, bool)
        or profile_version < 0
    ):
        raise ValueError("snapshot revision is invalid")
    validated: dict[str, object] = {
        "identity_id": identity_id,
        "profile_version": profile_version,
    }
    for field_name, max_length in IP_PROFILE_TEXT_LIMITS.items():
        field_value = value[field_name]
        if not isinstance(field_value, str) or len(field_value) > max_length:
            raise ValueError(f"snapshot {field_name} is invalid")
        if any(ord(character) < 32 or ord(character) == 127 for character in field_value):
            raise ValueError(f"snapshot {field_name} contains control characters")
        validated[field_name] = field_value
    return validated


def load_script_rewrite_configuration(
    conn: BusinessConnection,
) -> tuple[str, str, str]:
    try:
        config = SettingsRepository(conn).load_provider_config("deepseek")
    except SettingsUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DEEPSEEK_SETTINGS_UNAVAILABLE",
                "message": "本地配置暂不可用，请稍后重试。",
            },
        ) from exc
    api_key = config.get("api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DEEPSEEK_NOT_CONFIGURED",
                "message": (
                    "尚未配置 AI 改写服务。请管理员在"
                    + "「设置 → AI 改写」中保存 DeepSeek API Key。"
                ),
            },
        )
    return (
        (config.get("base_url") or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/"),
        api_key,
        config.get("model") or DEEPSEEK_DEFAULT_MODEL,
    )


def enqueue_script_rewrite_task(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    source_text: str,
    idempotency_key: str,
    identity_id: str | None = None,
) -> sqlite3.Row:
    require_not_auditor(
        conn,
        actor=actor,
        action="project.script_rewrite",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.script_rewrite",
    )
    text = validate_script_rewrite_text(source_text)
    # Fail fast before a task is accepted; the worker reloads the current
    # secret later and never persists it in request_json.
    load_script_rewrite_configuration(conn)
    profile_snapshot = (
        None
        if identity_id is None
        else _load_owned_ip_profile_snapshot(conn, actor=actor, identity_id=identity_id)
    )
    profile_json = None if profile_snapshot is None else _canonical_json(profile_snapshot)
    profile_hash = (
        None if profile_json is None else hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
    )
    request_payload = (
        {"text": text}
        if identity_id is None
        else {
            "text": text,
            "identity_id": identity_id,
            "ip_profile_snapshot": profile_snapshot,
        }
    )
    request_hash = hashlib.sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    replay = conn.execute(
        """
        SELECT * FROM script_rewrite_tasks
        WHERE project_id = %s AND idempotency_key = %s
        """,
        (project_id, idempotency_key),
    ).fetchone()
    if replay is not None:
        return _validated_idempotent_replay(
            replay,
            request_hash=request_hash,
            identity_id=identity_id,
        )

    active = conn.execute(
        """
        SELECT * FROM script_rewrite_tasks
        WHERE project_id = %s AND status IN ('PENDING','RUNNING')
        ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if active is not None:
        if str(active["request_hash"]) != request_hash:
            raise _script_rewrite_error(
                409,
                "SCRIPT_REWRITE_ALREADY_RUNNING",
                "该项目已有口播稿正在后台改写，请等待完成。",
            )
        return cast(sqlite3.Row, active)

    task_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO script_rewrite_tasks (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_json, identity_id,
            ip_profile_snapshot_json, ip_profile_hash, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'PENDING')
        ON CONFLICT DO NOTHING
        """,
        (
            task_id,
            project_id,
            actor.id,
            idempotency_key,
            request_hash,
            json.dumps(request_payload, ensure_ascii=False, sort_keys=True),
            identity_id,
            profile_json,
            profile_hash,
        ),
    )
    row = conn.execute(
        "SELECT * FROM script_rewrite_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT * FROM script_rewrite_tasks
            WHERE project_id = %s AND idempotency_key = %s
            """,
            (project_id, idempotency_key),
        ).fetchone()
        if row is not None:
            row = _validated_idempotent_replay(
                row,
                request_hash=request_hash,
                identity_id=identity_id,
            )
    if row is None:
        active = conn.execute(
            """
            SELECT * FROM script_rewrite_tasks
            WHERE project_id = %s AND status IN ('PENDING','RUNNING')
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if active is not None:
            active_identity_id = (
                None if active["identity_id"] is None else str(active["identity_id"])
            )
            if str(active["request_hash"]) != request_hash or active_identity_id != identity_id:
                raise _script_rewrite_error(
                    409,
                    "SCRIPT_REWRITE_ALREADY_RUNNING",
                    "该项目已有口播稿正在后台改写，请等待完成。",
                )
            row = active
    if row is None:
        raise _script_rewrite_error(
            409,
            "SCRIPT_REWRITE_ENQUEUE_CONFLICT",
            "改写任务状态已经变化，请重试。",
        )
    write_audit(
        conn,
        actor=actor,
        action="project.script_rewrite_enqueued",
        entity_type="script_rewrite_task",
        entity_id=str(row["id"]),
        metadata={
            "project_id": project_id,
            "request_hash": request_hash,
            "identity_id": identity_id,
            "ip_profile_hash": profile_hash,
        },
    )
    return cast(sqlite3.Row, row)


def _validated_idempotent_replay(
    row: sqlite3.Row,
    *,
    request_hash: str,
    identity_id: str | None,
) -> sqlite3.Row:
    stored_identity_id = None if row["identity_id"] is None else str(row["identity_id"])
    if str(row["request_hash"]) != request_hash or stored_identity_id != identity_id:
        raise _script_rewrite_error(
            409,
            "SCRIPT_REWRITE_IDEMPOTENCY_CONFLICT",
            "改写内容或人物档案已经变化，请重新提交。",
        )
    return row


def acquire_script_rewrite_task(
    conn: BusinessConnection,
    *,
    worker_id: str,
) -> ScriptRewriteTaskLease | None:
    now = _time_text(datetime.now(UTC))
    locked_until = _time_text(
        datetime.now(UTC) + timedelta(minutes=SCRIPT_REWRITE_TASK_LEASE_MINUTES)
    )
    conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'PENDING', locked_by = NULL, locked_until = NULL,
            error_code = NULL, error_message_redacted = NULL, retryable = 0,
            updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (now, now),
    )
    conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'SUBMISSION_UNCERTAIN', locked_by = NULL, locked_until = NULL,
            error_code = 'SCRIPT_REWRITE_SUBMISSION_UNCERTAIN',
            error_message_redacted = %s, retryable = 0,
            completed_at = %s, updated_at = %s
        WHERE status = 'RUNNING' AND provider_started_at IS NOT NULL
          AND locked_until IS NOT NULL AND locked_until <= %s
        """,
        (
            "AI 改写请求可能已经送达服务商，请人工确认后再决定是否重试。",
            now,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'RUNNING', attempt = attempt + 1,
            locked_by = %s, locked_until = %s,
            started_at = COALESCE(started_at, %s), updated_at = %s,
            error_code = NULL, error_message_redacted = NULL, retryable = 0
        WHERE id = (
            SELECT id FROM script_rewrite_tasks
            WHERE status = 'PENDING'
            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
        ) AND status = 'PENDING'
        RETURNING *
        """,
        (worker_id, locked_until, now, now),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ScriptRewriteTaskLease(
        id=str(row["id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        worker_id=worker_id,
        attempt=int(row["attempt"]),
    )


def prepare_script_rewrite_task(
    conn: BusinessConnection,
    *,
    lease: ScriptRewriteTaskLease,
) -> PreparedScriptRewrite:
    row = require_owned_script_rewrite_task(conn, lease)
    payload = json.loads(str(row["request_json"]))
    source_text = payload.get("text")
    if not isinstance(source_text, str):
        raise RuntimeError("script rewrite task text is unavailable")
    base_url, api_key, model = load_script_rewrite_configuration(conn)
    snapshot_raw = row["ip_profile_snapshot_json"]
    snapshot = None
    if snapshot_raw is not None:
        decoded = json.loads(str(snapshot_raw))
        if not isinstance(decoded, dict):
            raise RuntimeError("script rewrite IP profile snapshot is unavailable")
        snapshot = decoded
        expected_hash = hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()
        if expected_hash != str(row["ip_profile_hash"]):
            raise RuntimeError("script rewrite IP profile snapshot hash mismatch")
    return PreparedScriptRewrite(
        lease=lease,
        source_text=validate_script_rewrite_text(source_text),
        base_url=base_url,
        api_key=api_key,
        model=model,
        ip_profile_snapshot=snapshot,
    )


def mark_script_rewrite_submission_started(
    conn: BusinessConnection,
    *,
    lease: ScriptRewriteTaskLease,
) -> None:
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET provider_started_at = COALESCE(provider_started_at, %s), updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (now, now, lease.id, lease.worker_id),
    )
    if updated.rowcount != 1:
        raise RuntimeError("script rewrite task lease was lost")
    conn.commit()


def perform_script_rewrite_task(work: PreparedScriptRewrite) -> ScriptRewriteResult:
    rewritten = _request_deepseek(
        base_url=work.base_url,
        api_key=work.api_key,
        model=work.model,
        source_text=work.source_text,
        ip_profile_snapshot=work.ip_profile_snapshot,
    )
    return ScriptRewriteResult(
        rewritten_text=rewritten,
        provider="deepseek",
        model=work.model,
    )


def complete_script_rewrite_task(
    conn: BusinessConnection,
    *,
    lease: ScriptRewriteTaskLease,
    result: ScriptRewriteResult,
) -> None:
    now = _time_text(datetime.now(UTC))
    updated = conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = 'SUCCEEDED', result_json = %s,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s, retryable = 0
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (
            json.dumps(result.model_dump(), ensure_ascii=False, sort_keys=True),
            now,
            now,
            lease.id,
            lease.worker_id,
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("script rewrite task lease was lost")
    conn.commit()


def fail_script_rewrite_task(
    conn: BusinessConnection,
    *,
    lease: ScriptRewriteTaskLease,
    cause: Exception,
    submission_started: bool,
) -> None:
    code = "SCRIPT_REWRITE_TASK_FAILED"
    message = "AI 改写失败，请稍后重试。"
    retryable = True
    uncertain = False
    if isinstance(cause, HTTPException):
        detail: dict[str, Any] = cause.detail if isinstance(cause.detail, dict) else {}
        code = str(detail.get("code") or code)
        message = str(detail.get("message") or message)
        retryable = cause.status_code in {429, 502, 503, 504}
        uncertain = submission_started and cause.status_code == 504
    if uncertain:
        code = "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN"
        message = "AI 改写请求可能已经送达服务商，请人工确认后再决定是否重试。"
    now = _time_text(datetime.now(UTC))
    conn.execute(
        """
        UPDATE script_rewrite_tasks
        SET status = %s, error_code = %s,
            error_message_redacted = %s, retryable = %s,
            locked_by = NULL, locked_until = NULL,
            completed_at = %s, updated_at = %s
        WHERE id = %s AND status = 'RUNNING' AND locked_by = %s
        """,
        (
            "SUBMISSION_UNCERTAIN" if uncertain else "FAILED",
            code,
            message,
            0 if uncertain else (1 if retryable else 0),
            now,
            now,
            lease.id,
            lease.worker_id,
        ),
    )
    conn.commit()


def load_script_rewrite_task(
    conn: BusinessConnection,
    task_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM script_rewrite_tasks WHERE id = %s",
        (task_id,),
    ).fetchone()
    if row is None:
        raise _script_rewrite_error(
            404,
            "SCRIPT_REWRITE_TASK_NOT_FOUND",
            "改写任务不存在。",
        )
    return cast(sqlite3.Row, row)


def latest_script_rewrite_task(
    conn: BusinessConnection,
    *,
    project_id: str,
    identity_id: str | None = None,
    identity_scope: Literal["all", "identity", "none"] = "all",
) -> sqlite3.Row | None:
    if identity_scope == "all":
        query = """
            SELECT * FROM script_rewrite_tasks
            WHERE project_id = %s
            ORDER BY CASE WHEN status IN ('PENDING','RUNNING') THEN 0 ELSE 1 END,
                     created_at DESC, id DESC LIMIT 1
        """
        params: tuple[object, ...] = (project_id,)
    elif identity_scope == "identity":
        query = """
            SELECT * FROM script_rewrite_tasks
            WHERE project_id = %s AND identity_id = %s
            ORDER BY CASE WHEN status IN ('PENDING','RUNNING') THEN 0 ELSE 1 END,
                     created_at DESC, id DESC LIMIT 1
        """
        params = (project_id, identity_id)
    else:
        query = """
            SELECT * FROM script_rewrite_tasks
            WHERE project_id = %s AND identity_id IS NULL
            ORDER BY CASE WHEN status IN ('PENDING','RUNNING') THEN 0 ELSE 1 END,
                     created_at DESC, id DESC LIMIT 1
        """
        params = (project_id,)
    return cast(
        sqlite3.Row | None,
        conn.execute(query, params).fetchone(),
    )


def script_rewrite_task_result(row: sqlite3.Row) -> ScriptRewriteResult | None:
    raw = row["result_json"]
    if raw is None:
        return None
    try:
        return ScriptRewriteResult.model_validate(json.loads(str(raw)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def require_owned_script_rewrite_task(
    conn: BusinessConnection,
    lease: ScriptRewriteTaskLease,
) -> sqlite3.Row:
    row = load_script_rewrite_task(conn, lease.id)
    if str(row["status"]) != "RUNNING" or str(row["locked_by"]) != lease.worker_id:
        raise RuntimeError("script rewrite task lease was lost")
    return row


def _script_rewrite_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _time_text(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _request_deepseek(
    *,
    base_url: str,
    api_key: str,
    model: str,
    source_text: str,
    ip_profile_snapshot: dict[str, object] | None = None,
) -> str:
    messages: list[dict[str, str]] = [{"role": "system", "content": SCRIPT_REWRITE_SYSTEM_PROMPT}]
    if ip_profile_snapshot is not None:
        messages.append(
            {
                "role": "user",
                "content": _ip_profile_prompt(ip_profile_snapshot),
            }
        )
    messages.append({"role": "user", "content": f"请改写以下口播稿：\n\n{source_text}"})
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 1.3,
            "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
        }
    ).encode("utf-8")
    request = Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        logger.warning("DeepSeek rewrite failed with HTTP status %s", exc.code)
        message = (
            "AI 改写服务 API Key 无效或无权限，请检查设置。"
            if exc.code in (401, 403)
            else "AI 改写服务返回错误，请稍后重试。"
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "DEEPSEEK_REQUEST_FAILED", "message": message},
        ) from exc
    except (TimeoutError, URLError, OSError) as exc:
        logger.warning("DeepSeek rewrite failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=504,
            detail={
                "code": "DEEPSEEK_NETWORK_FAILED",
                "message": "连接 AI 改写服务失败，请检查网络后重试。",
            },
        ) from exc
    except (ValueError, KeyError) as exc:
        logger.warning("DeepSeek rewrite returned an unreadable payload")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DEEPSEEK_RESPONSE_INVALID",
                "message": "AI 改写服务返回内容异常，请重试。",
            },
        ) from exc

    try:
        content = str(body["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DEEPSEEK_RESPONSE_INVALID",
                "message": "AI 改写服务返回内容异常，请重试。",
            },
        ) from exc
    if not content:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DEEPSEEK_RESPONSE_EMPTY",
                "message": "AI 改写结果为空，请重试。",
            },
        )
    return content
