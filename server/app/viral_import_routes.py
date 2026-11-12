"""HTTP routes for durable viral-video project imports."""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, NoReturn, cast
from uuid import uuid4

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    status,
)
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.media import (
    DURATION_ROUNDING_TOLERANCE_SECONDS,
    MAX_DURATION_SECONDS,
    FFprobeVideoProbe,
    VideoProbe,
    VideoProbeFailed,
    VideoProbeUnavailable,
)
from app.media_routes import get_media_storage
from app.permissions import require_not_auditor
from app.storage import StorageAdapter, StorageBackendUnavailable
from app.viral_content_observations import record_content_source
from app.viral_import import (
    ViralImportRequest,
    ViralImportTaskResponse,
    enqueue_viral_import_task,
    load_viral_import_task,
    viral_import_task_response,
)
from app.viral_link import (
    DouyidouLinkClient,
    ResolvedViralLink,
    ViralLinkError,
    douyidou_link_client_from_settings,
    normalize_supported_link,
    supported_link_platform,
)
from app.viral_media import (
    CoverEnricher,
    UrlFetcher,
    ViralMediaDNSUnavailable,
    ViralMediaError,
    ViralMediaPipeline,
)
from app.viral_media_preparation import ViralMediaBusy
from app.viral_routes import (
    ViralVideoItem,
)
from app.viral_store import (
    LINK_IMPORT_CATEGORY,
    update_viral_cover,
    upsert_viral_videos,
)
from app.viral_tikhub import ViralSourceError, ViralVideo
from app.viral_wechat_link import WechatLinkClient, wechat_link_client_from_settings

router = APIRouter(prefix="/api/viral", tags=["viral"])
logger = logging.getLogger(__name__)
_LINK_RECEIPT_LEASE = timedelta(minutes=2)
# 与上传/拆解预检保持一致：30 秒硬上限 + 舍入容差。
_MAX_LINK_DURATION_SECONDS = MAX_DURATION_SECONDS + DURATION_ROUNDING_TOLERANCE_SECONDS
_MAX_LINK_DURATION_MS = int(_MAX_LINK_DURATION_SECONDS * 1000)


class ViralLinkResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2000)
    purpose: Literal["copy", "replica"]


class ViralLinkResolutionResponse(BaseModel):
    item: ViralVideoItem
    import_idempotency_key: str = Field(alias="importIdempotencyKey")


def _link_request_hash(normalized_url: str, purpose: str) -> str:
    payload = json.dumps(
        {"purpose": purpose, "url": normalized_url},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _claim_link_receipt(
    conn: BusinessConnection,
    *,
    owner_user_id: str,
    idempotency_key: str,
    normalized_url: str,
    purpose: str,
) -> tuple[Any, bool]:
    request_hash = _link_request_hash(normalized_url, purpose)
    receipt_id = str(uuid4())
    lease_owner = str(uuid4())
    lease_expires_at = (datetime.now(UTC) + _LINK_RECEIPT_LEASE).isoformat()
    conn.execute(
        """
        INSERT INTO viral_link_resolution_receipts (
            id, owner_user_id, idempotency_key, normalized_url, purpose,
            request_hash, status, lease_owner, lease_expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 'PREPARED', %s, %s)
        ON CONFLICT (owner_user_id, idempotency_key) DO NOTHING
        """,
        (
            receipt_id,
            owner_user_id,
            idempotency_key,
            normalized_url,
            purpose,
            request_hash,
            lease_owner,
            lease_expires_at,
        ),
    )
    row = conn.execute(
        """SELECT * FROM viral_link_resolution_receipts
        WHERE owner_user_id = %s AND idempotency_key = %s""",
        (owner_user_id, idempotency_key),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "VIRAL_LINK_RECEIPT_CONFLICT"})
    if str(row["request_hash"]) != request_hash:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VIRAL_LINK_IDEMPOTENCY_CONFLICT",
                "message": "幂等键已用于不同的视频链接请求。",
            },
        )
    claimed = str(row["id"]) == receipt_id
    if not claimed and str(row["status"]) == "PREPARED":
        claimed = (
            conn.execute(
                """
                UPDATE viral_link_resolution_receipts
                SET lease_owner = %s, lease_expires_at = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'PREPARED'
                  AND lease_expires_at::timestamptz < now()
                """,
                (lease_owner, lease_expires_at, row["id"]),
            ).rowcount
            == 1
        )
    elif not claimed and str(row["status"]) == "REQUEST_SENT":
        conn.execute(
            """
            UPDATE viral_link_resolution_receipts
            SET status = 'UNCERTAIN', completed_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND status = 'REQUEST_SENT'
              AND lease_expires_at::timestamptz < now()
            """,
            (row["id"],),
        )
    current = conn.execute(
        "SELECT * FROM viral_link_resolution_receipts WHERE id = %s", (row["id"],)
    ).fetchone()
    return current, claimed


def _mark_link_request_sent(conn: BusinessConnection, *, receipt_id: str, lease_owner: str) -> None:
    updated = conn.execute(
        """
        UPDATE viral_link_resolution_receipts
        SET status = 'REQUEST_SENT', updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'PREPARED' AND lease_owner = %s
        """,
        (receipt_id, lease_owner),
    )
    if updated.rowcount != 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VIRAL_LINK_IN_PROGRESS",
                "message": "该视频链接正在解析，请使用原请求稍后重试。",
            },
        )


def _receipt_replay(row: Any) -> ViralLinkResolutionResponse:
    status_value = str(row["status"])
    if status_value == "SUCCEEDED" and row["response_json"]:
        return ViralLinkResolutionResponse.model_validate_json(str(row["response_json"]))
    if status_value == "FAILED_SAFE":
        raise HTTPException(
            status_code=int(row["error_status"] or 422),
            detail={
                "code": str(row["error_code"] or "VIRAL_LINK_PARSE_FAILED"),
                "message": str(row["error_message_redacted"] or "视频链接无法解析。"),
            },
        )
    if status_value == "UNCERTAIN":
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_LINK_SUBMISSION_UNCERTAIN",
                "message": "视频链接解析结果未知，请勿更换幂等键重复调用；请上传 MP4/MOV 文件。",
            },
        )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "VIRAL_LINK_IN_PROGRESS",
            "message": "该视频链接正在解析，请使用原请求稍后重试。",
        },
    )


def _record_link_failure(
    conn: BusinessConnection, *, receipt_id: str, error: ViralLinkError
) -> None:
    status_value = "UNCERTAIN" if error.uncertain else "FAILED_SAFE"
    conn.execute(
        """
        UPDATE viral_link_resolution_receipts
        SET status = %s, error_status = %s, error_code = %s,
            error_message_redacted = %s, completed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND status = 'REQUEST_SENT'
        """,
        (status_value, error.status_code, error.code, error.message, receipt_id),
    )


def _resolved_video(resolved: ResolvedViralLink) -> ViralVideo:
    # 链接导入只保证媒体本身可复刻；缺失互动保留未知，不把 0 冒充平台统计。
    metadata = resolved.metadata
    return ViralVideo(
        platform=resolved.platform,
        video_id=resolved.video_id,
        category=LINK_IMPORT_CATEGORY,
        title=resolved.title,
        author=resolved.author,
        author_avatar=metadata.author_avatar,
        verified=metadata.verified,
        cover_url=resolved.cover_url,
        duration_ms=resolved.duration_ms,
        likes=metadata.likes,
        comments=metadata.comments,
        shares=metadata.shares,
        collects=metadata.collects,
        published_at=metadata.published_at,
        published_display=None,
        like_display=None,
        tags=list(metadata.tags),
        play_url=resolved.video_url,
        audio_url=resolved.audio_url,
        native={
            "source_description": resolved.source_description,
            "link_resolved": True,
            # 解析通道回填的溯源键（视频号 share_url/export_id）原样并入：
            # 媒体管线重取直链、统计刷新回落都靠这两把钥匙。
            **dict(resolved.native),
        },
    )


def preflight_resolved_media(
    resolved: ResolvedViralLink, *, purpose: str, storage: StorageAdapter
) -> None:
    # 链接解析已带时长时，先拦截超过 30 秒上限的参考视频，避免无谓下载与后续静默失败；
    # 提取文案（copy）以原视频音轨转写，不受复刻的 30 秒上限约束。
    if purpose != "copy" and resolved.duration_ms > _MAX_LINK_DURATION_MS:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_DURATION_EXCEEDED",
            f"视频时长约 {round(resolved.duration_ms / 1000)} 秒，超过 30 秒上限，"
            "无法复刻；请截取 30 秒以内片段后重试。",
            retryable=False,
        )
    # 文案链路直接缓存解析返回的音频（2026-09-30 拍板：上游音频已可直接用于转写），
    # 不再为抽口播强制下载整条视频；复刻仍以完整视频为准。解析未带音频地址时管线
    # 自动回落到视频下载（见 viral_media._resolve_kind）。
    prefer = "audio" if purpose == "copy" else "video"

    def validate(content: Path, kind: str, content_type: str | None) -> None:
        validate_resolved_media_content(
            content,
            kind=kind,
            content_type=content_type,
            probe=FFprobeVideoProbe(),
            purpose=purpose,
        )

    try:
        ViralMediaPipeline(
            client=None,
            storage=storage,
            # 2026-09-30 拍板：链接下载不再套用上传体的 50MB 上限（长视频/长音频照常
            # 拉取），25 秒超时保留；UrlFetcher 自带的 512MB 防滥用硬顶仍在。
            fetcher=UrlFetcher(timeout_seconds=25.0),
            validator=validate,
            shared=True,
        ).fetch(_resolved_video(resolved), prefer=prefer)
    except ViralLinkError:
        raise
    except ViralMediaBusy as exc:
        raise ViralLinkError(
            503,
            "VIRAL_MEDIA_PREPARATION_BUSY",
            "该视频素材正在准备中，请稍后重试。",
            retryable=True,
        ) from exc
    except ViralMediaDNSUnavailable as exc:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE",
            "当前网络无法获取视频媒体地址，请检查代理或 DNS 设置后重试。",
            retryable=False,
        ) from exc
    except ViralMediaError as exc:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体不可用，请上传 MP4 或 MOV 文件。",
            retryable=False,
        ) from exc
    except (ViralSourceError, StorageBackendUnavailable) as exc:
        raise ViralLinkError(
            503,
            "VIRAL_MEDIA_PREPARATION_FAILED",
            "云端素材准备暂未完成，请稍后重试。",
            retryable=True,
        ) from exc


def validate_resolved_media_content(
    content: bytes | Path,
    *,
    kind: str,
    content_type: str | None,
    probe: VideoProbe,
    purpose: str,
) -> None:
    source_path = content if isinstance(content, Path) else None
    if source_path is not None:
        with source_path.open("rb") as source:
            content = source.read(12)
    content = cast(bytes, content)
    normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
    expected_prefix = "audio/" if kind == "audio" else "video/"
    mp4_container = len(content) >= 12 and content[4:8] == b"ftyp"
    if kind == "audio":
        magic_valid = (
            mp4_container
            or content.startswith(b"ID3")
            or (len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0)
        )
    else:
        magic_valid = mp4_container
    if not normalized_type.startswith(expected_prefix) or not magic_valid:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体格式无效，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    try:
        filename = "source.m4a" if mp4_container else "source.mp3"
        metadata = (
            cast(FFprobeVideoProbe, probe).probe_file(source_path)
            if source_path is not None
            else probe.probe(content, filename=filename if kind == "audio" else "source.mp4")
        )
    except VideoProbeUnavailable as exc:
        raise ViralLinkError(
            503,
            "VIRAL_LINK_MEDIA_PROBE_UNAVAILABLE",
            "视频检测服务暂不可用，请上传 MP4 或 MOV 文件。",
            retryable=False,
        ) from exc
    except VideoProbeFailed as exc:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体无法播放，请上传 MP4 或 MOV 文件。",
            retryable=False,
        ) from exc
    if metadata.duration_seconds <= 0:
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_INVALID",
            "链接媒体无法播放，请上传 MP4 或 MOV 文件。",
            retryable=False,
        )
    # 仅复刻链路的视频受 30 秒上限约束；提取文案（copy）与口播声音样本（audio）
    # 时长可较长，不在此拦截。
    if (
        purpose != "copy"
        and kind == "video"
        and metadata.duration_seconds > _MAX_LINK_DURATION_SECONDS
    ):
        raise ViralLinkError(
            422,
            "VIRAL_LINK_MEDIA_DURATION_EXCEEDED",
            f"视频实测时长 {round(metadata.duration_seconds)} 秒，超过 30 秒上限，"
            "无法复刻；请截取 30 秒以内片段后重试。",
            retryable=False,
        )


@router.post(
    "/link-resolutions",
    response_model=ViralLinkResolutionResponse,
    responses={409: {}, 410: {}, 502: {}, 503: {}, 504: {}},
)
def resolve_viral_link(
    request: ViralLinkResolutionRequest,
    db: BusinessDbDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ViralLinkResolutionResponse:
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_LINK_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    try:
        normalized_url = normalize_supported_link(request.url)
        with db.write() as (conn, actor):
            with conn:
                require_not_auditor(
                    conn,
                    actor=actor,
                    action="viral.link.resolve",
                    entity_type="viral_link",
                    entity_id=supported_link_platform(normalized_url),
                )
                receipt, created = _claim_link_receipt(
                    conn,
                    owner_user_id=actor.id,
                    idempotency_key=key,
                    normalized_url=normalized_url,
                    purpose=request.purpose,
                )
            if created:
                with conn:
                    # 按域名分发解析通道：抖音/小红书走去水印网关，视频号走
                    # 登录态解析链（三步外呼在服务端完成，客户无感知差异）。
                    resolver: DouyidouLinkClient | WechatLinkClient
                    if supported_link_platform(normalized_url) == "wechat_channels":
                        resolver = wechat_link_client_from_settings(conn)
                    else:
                        resolver = douyidou_link_client_from_settings(conn)
                    from app.usage_billing import accept_operation

                    accept_operation(
                        conn,
                        user_id=actor.id,
                        service="link_resolution",
                        source_id=str(receipt["id"]),
                        units=1,
                    )
        if not created:
            return _receipt_replay(receipt)
        with db.write() as (conn, sending_actor):
            with conn:
                if sending_actor.id != actor.id:
                    raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
                _mark_link_request_sent(
                    conn,
                    receipt_id=str(receipt["id"]),
                    lease_owner=str(receipt["lease_owner"]),
                )
                from app.usage_billing import begin_source_attempt

                begin_source_attempt(conn, str(receipt["id"]))
        resolved = resolver.resolve(normalized_url, purpose=request.purpose)
        with db.write() as (conn, _cost_actor):
            from app.usage_billing import complete_source_attempt

            complete_source_attempt(conn, str(receipt["id"]), usage=1)
        with db.write() as (conn, media_actor):
            if media_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            storage = get_media_storage(conn)
        preflight_resolved_media(resolved, purpose=request.purpose, storage=storage)
    except ViralLinkError as exc:
        if "receipt" in locals():
            with db.write() as (conn, _actor):
                with conn:
                    _record_link_failure(conn, receipt_id=str(receipt["id"]), error=exc)
                    from app.usage_billing import complete_source_attempt, finish_source

                    complete_source_attempt(conn, str(receipt["id"]), usage=None)
                    finish_source(conn, str(receipt["id"]), units=0, succeeded=False)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    video = _resolved_video(resolved)
    import_key = (
        "viral-link-"
        + hashlib.sha256(
            f"{actor.id}:{video.platform}:{video.video_id}:{request.purpose}".encode()
        ).hexdigest()
    )
    response = ViralLinkResolutionResponse(
        item=ViralVideoItem(**video.to_client_dict()),
        importIdempotencyKey=import_key,
    )
    with db.write() as (conn, completed_actor):
        with conn:
            if completed_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            updated = conn.execute(
                """
                UPDATE viral_link_resolution_receipts
                SET status = 'SUCCEEDED', response_json = %s,
                    completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s AND status = 'REQUEST_SENT' AND lease_owner = %s
                """,
                (
                    response.model_dump_json(by_alias=True),
                    receipt["id"],
                    receipt["lease_owner"],
                ),
            )
            if updated.rowcount != 1:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "VIRAL_LINK_SUBMISSION_UNCERTAIN",
                        "message": "视频链接解析结果未知，请上传 MP4/MOV 文件。",
                    },
                )
            from app.usage_billing import finish_source

            finish_source(conn, str(receipt["id"]), units=1, succeeded=True)
            # 客户导入也是内容池来源：不登记会让漏斗"本期采集"漏掉这批视频
            # （2026-10-08 拍板：漏斗口径=全来源进 cohort）。keyword 留空串，
            # 关键词榜按 keyword<>'' 排除导入来源。
            inserted = upsert_viral_videos(conn, [video], commit=False)
            record_content_source(
                conn,
                batch_id=f"import:{receipt['id']}",
                source_kind="customer_import",
                platform=video.platform,
                video_id=video.video_id,
                keyword="",
                is_new=(video.platform, video.video_id) in inserted,
            )
    # ADM-C04：导入路径同样把源站封面归档为自有副本——此前只有采集/转存跑
    # CoverEnricher，链接导入的封面停留在会过期的源站链接。失败保留源站链接
    # 兜底（CoverEnricher 内部已容错），不阻塞解析响应。
    try:
        enriched_cover = CoverEnricher(
            storage=storage, fetcher=UrlFetcher(max_bytes=10 * 1024 * 1024), metered=True
        ).enrich(video)
        if getattr(enriched_cover, "cover_key", None):
            with db.write() as (conn, _cover_actor):
                with conn:
                    update_viral_cover(
                        conn,
                        platform=video.platform,
                        video_id=video.video_id,
                        cover_key=str(enriched_cover.cover_key),
                    )
    except Exception as exc:
        logger.warning(
            "链接导入封面归档失败（保留源站链接兜底）%s/%s: %s",
            video.platform,
            video.video_id,
            exc,
        )
    return response


@router.post(
    "/import-tasks",
    response_model=ViralImportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
    include_in_schema=False,
)
@router.post(
    "/videos/import-tasks",
    response_model=ViralImportTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_viral_import_task(
    request: ViralImportRequest,
    db: BusinessDbDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralImportTaskResponse:
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_IMPORT_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    with db.write() as (conn, actor):
        with conn:
            row = enqueue_viral_import_task(conn, actor=actor, request=request, idempotency_key=key)
        return viral_import_task_response(row)


@router.get("/import-tasks/{task_id}", response_model=ViralImportTaskResponse)
def read_viral_import_task(
    task_id: str, conn: Database, actor: AuthenticatedUser
) -> ViralImportTaskResponse:
    row = load_viral_import_task(conn, task_id)
    if str(row["owner_user_id"]) != actor.id:
        raise HTTPException(
            status_code=404,
            detail={"code": "VIRAL_IMPORT_TASK_NOT_FOUND", "message": "导入任务不存在。"},
        )
    return viral_import_task_response(row)


# ------------------------------------------------- 文案（本地抽音轨上传已退役）


@router.post(
    "/videos/copy",
    status_code=status.HTTP_410_GONE,
    include_in_schema=False,
    response_model=None,
)
def extract_viral_video_copy() -> NoReturn:
    """客户端音轨上传已退役（2026-11 爆款链路改造 WP2）：提取文案统一走服务端导入转写.

    保留路由壳返回 410 引导旧桌面客户端升级，而不是裸 404。2026-11-11 极简化
    之后文案正文随本人转写任务 ``result.text`` 直接下发（claim 交付已退役为
    410 壳），本端点不再参与计费。上传受理原语（register_viral_copy_source
    与 viral_copy_uploads）保留在原模块，服务存量凭据回收与历史任务夹具，
    生产代码不再触达。
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "VIRAL_COPY_UPLOAD_RETIRED",
            "message": "该提取方式已下线，请升级客户端后使用新版提取文案。",
        },
    )
