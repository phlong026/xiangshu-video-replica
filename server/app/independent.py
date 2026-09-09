"""Standalone video creation domain (C2 独立创作).

文生（T2V）/ 图生（I2V）/ 参考生（R2V）视频的脱离项目上下文创建通道。
批次挂在 ``generation_batches``（project_id 为 NULL、creation_kind 为
"independent"）上，从建批那一刻起即复用复刻流的既有管线：公平队列、
worker 提交/轮询/归档、钱包按秒计费（RESERVE/SETTLE/RELEASE）、任务中心
列表与对账。本模块只负责创建路径的准入与快照，不重建任何管线实体。

供应商中立：表、行与对客文案不得出现数据源供应商名称（红线同 oral 域）。

扩展模式（尾帧 / T2V / R2V）的真实提交由
``runtime_settings.h3_extended_modes_enabled`` 总开关门禁：协议构造与
测试已就绪，但按 docs/短视频复刻桌面端开发说明.md §21.3 必须先完成
供应商 API 核对（付费探针）再由管理端打开。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal
from uuid import uuid4

import psycopg.errors as psycopg_errors
from pydantic import BaseModel, ConfigDict, Field

from app.bootstrap import is_customer_production
from app.db_portable import BusinessConnection
from app.generation import (
    H3_MODEL,
    BatchResult,
    H3ProviderSettingsUnavailable,
    _enforce_acceptance_generation_limit,
    _reserve_generation_credit,
    content_hash,
    ensure_user_queue_cursor,
    generation_error,
    get_generation_batch,
    metaso_h3_provider_from_settings,
    read_runtime_limits,
    require_cos_first_frame_storage,
)
from app.operation_costs import snapshot_generation_rates
from app.permissions import require_asset_access, require_not_auditor

IndependentMode = Literal["t2v", "i2v", "r2v"]

_MODE_UPPPER = {"t2v": "T2V", "i2v": "I2V", "r2v": "R2V"}
# 首帧/尾帧/参考图允许的资产类别：用户素材图片与既有图片资产通道。
_FRAME_IMAGE_KINDS = {
    "image",
    "material_image",
    "first_frame",
    "character_source_image",
    "character_contact_sheet",
}
MAX_REFERENCE_IMAGES = 4


class IndependentVideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: IndependentMode
    prompt_text: str = Field(min_length=1, max_length=4000)
    first_frame_asset_id: str | None = Field(default=None, min_length=1)
    last_frame_asset_id: str | None = Field(default=None, min_length=1)
    reference_asset_ids: list[str] = Field(default_factory=list, max_length=MAX_REFERENCE_IMAGES)
    output_duration_seconds: int = Field(ge=4, le=15)
    resolution: Literal["768P", "2K"] = "768P"
    ratio: Literal["adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"] = "adaptive"
    quantity: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    provider: Literal["fake_h3", "metaso"] = "fake_h3"


class IndependentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extended_modes_enabled: bool
    t2v_enabled: bool
    i2v_enabled: bool
    r2v_enabled: bool
    last_frame_enabled: bool
    max_reference_images: int = MAX_REFERENCE_IMAGES
    max_quantity: int


def _extended_modes_enabled(conn: BusinessConnection) -> bool:
    row = conn.execute(
        "SELECT h3_extended_modes_enabled FROM runtime_settings WHERE id = 1"
    ).fetchone()
    return bool(row and row["h3_extended_modes_enabled"])


def read_independent_capabilities(conn: BusinessConnection) -> IndependentCapabilities:
    runtime = read_runtime_limits(conn)
    extended = _extended_modes_enabled(conn)
    return IndependentCapabilities(
        extended_modes_enabled=extended,
        t2v_enabled=extended,
        i2v_enabled=True,
        r2v_enabled=extended,
        last_frame_enabled=extended,
        max_quantity=runtime["max_generation_count_per_batch"],
    )


def _independent_request_hash(request: IndependentVideoRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    return content_hash(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _find_independent_batch(
    conn: BusinessConnection, *, actor_id: str, key: str
) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        """
        SELECT id, request_hash
        FROM generation_batches
        WHERE created_by_user_id = %s AND project_id IS NULL AND idempotency_key = %s
        """,
        (actor_id, key),
    ).fetchone()
    return row


def _validated_frame_asset(
    conn: BusinessConnection,
    *,
    actor: Any,
    asset_id: str,
    role: str,
    provider: str,
) -> dict[str, str]:
    asset = require_asset_access(conn, actor=actor, asset_id=asset_id, action="independent.create")
    if str(asset["kind"]) not in _FRAME_IMAGE_KINDS:
        raise generation_error(
            422,
            "INDEPENDENT_ASSET_KIND_UNSUPPORTED",
            f"{role} must be an image asset.",
        )
    storage_uri = str(asset["storage_uri"] or "")
    if not storage_uri:
        raise generation_error(
            422,
            "INDEPENDENT_ASSET_STORAGE_MISSING",
            f"{role} asset has no stored content yet.",
        )
    if provider == "metaso":
        require_cos_first_frame_storage(conn, storage_uri=storage_uri)
    return {"asset_id": str(asset["id"]), "uri": storage_uri}


def create_independent_batch(
    conn: BusinessConnection,
    *,
    actor: Any,
    request: IndependentVideoRequest,
) -> BatchResult:
    require_not_auditor(
        conn,
        actor=actor,
        action="independent.create",
        entity_type="generation_batch",
        entity_id="independent",
    )

    mode_upper = _MODE_UPPPER[request.mode]
    extended_enabled = _extended_modes_enabled(conn)
    uses_tail_frame = request.last_frame_asset_id is not None
    uses_references = bool(request.reference_asset_ids)
    if (request.mode in {"t2v", "r2v"} or uses_tail_frame) and not extended_enabled:
        raise generation_error(
            409,
            "EXTENDED_MODE_PENDING_VERIFICATION",
            "该模式需要完成供应商核对后开放，敬请期待。",
        )

    # 模式与素材矩阵（H3 输入互斥规则）。
    if request.mode == "t2v" and (request.first_frame_asset_id or uses_tail_frame):
        raise generation_error(
            422, "INDEPENDENT_MODE_ASSET_CONFLICT", "文生视频不能携带首帧或尾帧。"
        )
    if request.mode == "i2v" and not request.first_frame_asset_id:
        raise generation_error(
            422, "INDEPENDENT_FIRST_FRAME_REQUIRED", "图生视频需要选择首帧图片。"
        )
    if request.mode == "r2v":
        if request.first_frame_asset_id or uses_tail_frame:
            raise generation_error(
                422,
                "INDEPENDENT_MODE_ASSET_CONFLICT",
                "参考生视频不能携带首帧或尾帧。",
            )
        if not uses_references:
            raise generation_error(
                422, "INDEPENDENT_REFERENCE_REQUIRED", "参考生视频至少选择一张参考图。"
            )
        if len(set(request.reference_asset_ids)) != len(request.reference_asset_ids):
            raise generation_error(
                422,
                "INDEPENDENT_REFERENCE_DUPLICATE",
                "参考图不能重复选择。",
            )

    # 与复刻流同源的生产红线：客户生产禁止模拟任务；metaso 需配置就绪并
    # 遵守付费试用限额。仅对“真正的新提交”生效，幂等回放在此之前返回。
    if request.provider == "fake_h3" and is_customer_production():
        raise generation_error(
            503,
            "FAKE_H3_PROVIDER_FORBIDDEN",
            "Customer production cannot create simulated H3 generation tasks.",
        )
    if request.provider == "metaso":
        _enforce_acceptance_generation_limit(
            conn,
            user_id=actor.id,
            requested_quantity=request.quantity,
        )

    request_hash = _independent_request_hash(request)
    existing = _find_independent_batch(conn, actor_id=actor.id, key=request.idempotency_key)
    if existing is not None:
        if str(existing["request_hash"]) != request_hash:
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This idempotency key was already used for a different request.",
            )
        return get_generation_batch(conn, batch_id=str(existing["id"]), actor=actor)

    runtime = read_runtime_limits(conn)
    if request.quantity > runtime["max_generation_count_per_batch"]:
        raise generation_error(
            422,
            "QUANTITY_EXCEEDS_LIMIT",
            f"quantity must be less than or equal to {runtime['max_generation_count_per_batch']}",
        )
    if request.provider == "metaso":
        try:
            metaso_h3_provider_from_settings(conn)
        except H3ProviderSettingsUnavailable as exc:
            raise generation_error(
                503,
                "METASO_SETTINGS_UNAVAILABLE",
                "视频生成服务尚未配置完成，请联系管理员。",
            ) from exc

    first_frame = (
        _validated_frame_asset(
            conn,
            actor=actor,
            asset_id=request.first_frame_asset_id,
            role="First frame",
            provider=request.provider,
        )
        if request.first_frame_asset_id
        else None
    )
    last_frame = (
        _validated_frame_asset(
            conn,
            actor=actor,
            asset_id=request.last_frame_asset_id,
            role="Last frame",
            provider=request.provider,
        )
        if request.last_frame_asset_id
        else None
    )
    reference_images = [
        {
            **_validated_frame_asset(
                conn,
                actor=actor,
                asset_id=asset_id,
                role="Reference image",
                provider=request.provider,
            ),
            "name": f"ref-{index + 1}",
        }
        for index, asset_id in enumerate(request.reference_asset_ids)
    ]

    try:
        conn.execute("BEGIN IMMEDIATE")
        concurrent_existing = _find_independent_batch(
            conn, actor_id=actor.id, key=request.idempotency_key
        )
        if concurrent_existing is not None:
            conn.rollback()
            if str(concurrent_existing["request_hash"]) != request_hash:
                raise generation_error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This idempotency key was already used for a different request.",
                )
            return get_generation_batch(conn, batch_id=str(concurrent_existing["id"]), actor=actor)

        batch_id = str(uuid4())
        request_snapshot = {
            "schema_version": "independent.v1",
            "generation_mode": mode_upper,
            "quantity": request.quantity,
            "prompt_text": request.prompt_text,
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            "ratio": request.ratio,
            "provider": request.provider,
            "model": H3_MODEL,
            "first_frame_asset_id": request.first_frame_asset_id,
            "last_frame_asset_id": request.last_frame_asset_id,
            "reference_asset_ids": list(request.reference_asset_ids),
        }
        task_prompt_snapshot: dict[str, Any] = {
            "schema_version": "independent.v1",
            "generation_mode": mode_upper,
            "prompt_text": request.prompt_text,
            "output_duration_seconds": request.output_duration_seconds,
            "resolution": request.resolution,
            "ratio": request.ratio,
            "first_frame_uri": first_frame["uri"] if first_frame else None,
            "last_frame_uri": last_frame["uri"] if last_frame else None,
            "reference_images": reference_images,
        }
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
            VALUES (%s, NULL, %s, %s, %s, %s, 'independent', 'QUEUED')
            """,
            (
                batch_id,
                actor.id,
                request.idempotency_key,
                request_hash,
                json.dumps(request_snapshot, ensure_ascii=True, sort_keys=True),
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
                    prompt_snapshot_json,
                    next_poll_at,
                    billed_seconds
                )
                VALUES (%s, %s, %s, %s, %s, 'PENDING', 'PENDING', 'PENDING', %s,
                        CURRENT_TIMESTAMP, %s)
                """,
                (
                    task_id,
                    batch_id,
                    mode_upper,
                    request.provider,
                    H3_MODEL,
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
            )
        ensure_user_queue_cursor(conn, user_id=actor.id)
        conn.commit()
    except (sqlite3.IntegrityError, psycopg_errors.UniqueViolation):
        # SQLite：写锁保证事务内复查可见，可安全回放；
        # PG：NULL project 不受 UNIQUE 约束保护，由 075 的部分唯一索引兜底。
        # fenced 事务冲突后已中止，无法在同事务内回放——返回 409 让客户端
        # 重试（重试会命中幂等回放）。
        conn.rollback()
        if conn.is_postgres:
            raise generation_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "Duplicate concurrent submission; retry to fetch the same batch.",
            ) from None
        existing = _find_independent_batch(conn, actor_id=actor.id, key=request.idempotency_key)
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
    return get_generation_batch(conn, batch_id=batch_id, actor=actor)
