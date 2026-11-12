"""平台培训素材库（IA 改版第 5 项）：后台统一上传，客户只读。

素材库页面从「用户自传素材」整体替换为本模块的数据源（产品已拍板彻底
替换，2026-11-01）。用户自传链路（参考视频 / 人物图 / 发布封面）不走
本表，仍由 ``materials``/``media`` 域承载。

2026-11-12 网盘改造（docs/培训素材123网盘存储方案-2026-11-12.md）：存储
车道整体切换为 123 网盘（``Pan123Storage``），格式白名单扩为视频/音频/图片/
文档四组，新增必填标签与按标签筛选；本地车道签名下载端点随车道删除，
客户与管理的取链统一为服务端签名的防盗链直链。

两组端点、同一张表（与公告同构的「admin 管理 + 客户只读」范式）：

- **管理端** ``/api/control/training-materials``：multipart 上传（四组白名单）、
  改标题/说明/排序/标签/上下架、软删；另有 ``GET /{id}/direct-link`` 取复制
  用直链（不递增下载计数）。写走四段契约（真实管理员会话、Idempotency-Key、
  confirm、reason），审计 ``training_material.*``。
- **客户侧** ``/api/training-materials``：上架素材分页列表（kind/tag 筛选，
  响应带分页前聚合的标签面）；``POST /{id}/download-url`` 签发短时效防盗链
  直链（30 分钟）并递增下载计数。

并发与账号边界（方案 §3.4，评审 r2/r3 闭环）：

- 上传先传网盘、后写业务行——业务提交事务持统一互斥锁
  ``audit:provider:pan123`` 并**重读配置指纹**，与上传时记录的版本比对，
  配置已变化（换号/换密钥）则拒绝入库并按孤儿对象口径记日志；
- ``enforce_pan123_config_guard``（settings.py）在配置保存侧持同一把锁：
  有素材行禁换账号、放行时同事务清除共享 token。两个提交顺序都不会留下
  绑定旧账号的素材行。

``kind`` 由上传白名单从文件类型推导，不接受手填，避免「视频栏目里出现
PDF」这类栏目错位。标签服务端规范化（1-5 个、单个 ≤20 字、保序去重），
规范化结果进入写契约 → 经幂等指纹参与重放判定。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
)
from starlette.concurrency import run_in_threadpool

from app.admin_auth_routes import AdminReader, ContentWriter
from app.admin_customer_routes import _write_with_idempotency
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.admin_write_contract import require_write_contract
from app.auth import AuthenticatedUser, Database
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.pan123_client import (
    Pan123Client,
    Pan123FileMissing,
    Pan123Storage,
)
from app.settings import PAN123_PROVIDER_NAME, SettingsRepository
from app.sql_pagination import page_bounds

router = APIRouter(prefix="/api", tags=["training-materials"])
logger = logging.getLogger(__name__)

KINDS = ("video", "audio", "image", "doc")
_KIND_LABELS = {"video": "视频", "audio": "音频", "image": "图片", "doc": "文档"}
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000
MAX_TAGS = 5
MAX_TAG_CHARS = 20

# 「后缀 + content-type 成对登记」防混搭（与 materials.ALLOWED_UPLOADS 同一纪律）。
# kind 由文件类型推导四组，不接受管理员手填；白名单只收录浏览器可直接消费的
# 格式——上传后在软件内尽力预览，失败可下载（方案 §4）。
TRAINING_UPLOADS: dict[tuple[str, str], tuple[str, str]] = {
    (".mp4", "video/mp4"): ("video", ".mp4"),
    (".mov", "video/quicktime"): ("video", ".mov"),
    (".webm", "video/webm"): ("video", ".webm"),
    (".mp3", "audio/mpeg"): ("audio", ".mp3"),
    (".m4a", "audio/mp4"): ("audio", ".m4a"),
    (".wav", "audio/wav"): ("audio", ".wav"),
    (".jpg", "image/jpeg"): ("image", ".jpg"),
    (".jpeg", "image/jpeg"): ("image", ".jpeg"),
    (".png", "image/png"): ("image", ".png"),
    (".gif", "image/gif"): ("image", ".gif"),
    (".webp", "image/webp"): ("image", ".webp"),
    (".pdf", "application/pdf"): ("doc", ".pdf"),
    (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ): ("doc", ".docx"),
    (".doc", "application/msword"): ("doc", ".doc"),
    (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ): ("doc", ".xlsx"),
    (".xls", "application/vnd.ms-excel"): ("doc", ".xls"),
    (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ): ("doc", ".pptx"),
    (".ppt", "application/vnd.ms-powerpoint"): ("doc", ".ppt"),
}
TRAINING_MATERIAL_MAX_BYTES = 200 * 1024 * 1024  # 与媒体车道 MAX_UPLOAD_BYTES 对齐
DOWNLOAD_URL_TTL_SECONDS = 30 * 60  # 防盗链直链有效期（方案 §7.2 短时效兜底）
_UPLOAD_CHUNK_BYTES = 1024 * 1024
# 统一互斥锁：配置保存（护栏）与上传业务提交共用，见方案 §3.4。
PAN123_CONFIG_LOCK_KEY = "audit:provider:pan123"

_UNAVAILABLE_CODE = "TRAINING_MATERIAL_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "培训素材库需要 PostgreSQL 运行时。"
_STORAGE_UNAVAILABLE_CODE = "TRAINING_MATERIAL_STORAGE_UNAVAILABLE"
_STORAGE_UNAVAILABLE_MESSAGE = "培训素材存储通道未配置，请联系管理员。"
_TAG_CHARS = re.compile(r"^[\w\u4e00-\u9fa5 \-]+$", re.UNICODE)


def _normalize_tags(raw: list[str] | None) -> list[str]:
    """标签规范化：strip → 折叠空白 → 去空 → 保序去重；数量与单长超限 422。

    必填：空列表同样拒绝（上传必填；编辑传 tags = 修改，不传 = 不动）。
    """
    seen: list[str] = []
    for item in raw or []:
        tag = re.sub(r"\s+", " ", str(item)).strip()
        if not tag or tag in seen:
            continue
        if len(tag) > MAX_TAG_CHARS:
            raise _http(
                422,
                "TRAINING_MATERIAL_VALIDATION_FAILED",
                f"单个标签不超过 {MAX_TAG_CHARS} 字。",
            )
        if not _TAG_CHARS.match(tag):
            raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "标签含不支持的字符。")
        seen.append(tag)
    if not (1 <= len(seen) <= MAX_TAGS):
        raise _http(
            422,
            "TRAINING_MATERIAL_VALIDATION_FAILED",
            f"请填写 1-{MAX_TAGS} 个标签（用于分类筛选）。",
        )
    return seen


def get_training_material_storage() -> tuple[Pan123Storage, str]:
    """解析培训域唯一存储车道（123网盘）及其配置指纹（方案 §3.4）。

    返回 (storage, config_version)；配置缺失/不完整 → 503 中性文案（不暴露
    供应商名）。指纹供上传提交时的复核（换号/换密钥即拒绝入库）。
    """
    with pg_transaction() as pg:
        repo = SettingsRepository(BusinessConnection.postgres(pg))
        config = repo.load_provider_config(PAN123_PROVIDER_NAME)
        version = repo.provider_config_version(PAN123_PROVIDER_NAME)
    required = ("client_id", "client_secret", "uid", "sign_secret", "folder_id")
    if any(not str(config.get(field, "")).strip() for field in required):
        raise _http(503, _STORAGE_UNAVAILABLE_CODE, _STORAGE_UNAVAILABLE_MESSAGE)
    return Pan123Storage(Pan123Client(config, config_version=version)), version


def _ts(value: object) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _material_row(row: Any) -> dict[str, object]:
    raw_tags = row["tags"]
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "description": str(row["description"] or ""),
        "kind": str(row["kind"]),
        "kind_label": _KIND_LABELS.get(str(row["kind"]), str(row["kind"])),
        "content_type": str(row["content_type"]),
        "file_suffix": str(row["file_suffix"]),
        "size_bytes": int(row["size_bytes"]),
        "tags": tags,
        "sort_order": int(row["sort_order"]),
        "visible": bool(row["visible"]),
        "download_count": int(row["download_count"]),
        # _NamedRow 无 .get()；客户列表 SELECT * 不带创建者列，缺列回退空串。
        "created_by": str(getattr(row, "created_by", "") or ""),
        "created_at": _ts(row["created_at"]),
        "updated_at": _ts(row["updated_at"]),
    }


def _visible_filters(kind: str, tag: str) -> tuple[str, tuple[object, ...]]:
    clauses = ["deleted_at IS NULL", "visible = 1"]
    params: list[object] = []
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if tag:
        clauses.append("tags @> %s::jsonb")
        params.append(json.dumps([tag], ensure_ascii=False))
    return " AND ".join(clauses), tuple(params)


# --- 客户侧 -------------------------------------------------------------------


@router.get("/training-materials")
def list_training_materials(
    conn: Database,
    actor: AuthenticatedUser,
    kind: str = "",
    tag: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 24,
) -> dict[str, object]:
    """上架素材（置顶序优先，新→旧次之）。只读，客户无任何写入口。

    标签面（tag_facet）按**分页前的全量可见集合**聚合（评审 r1-P1-5）：
    翻页之外的标签同样可选，筛选 chips 不随分页缩水。
    """
    del conn, actor  # 鉴权由依赖完成；列表查询只依赖数据库时钟
    if kind and kind not in KINDS:
        raise _http(422, "TRAINING_MATERIAL_INVALID_KIND", "素材类型无效。")
    where, params = _visible_filters(kind, tag)
    offset = (page - 1) * page_size
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            rows = business.execute(
                f"""
                SELECT * FROM training_materials
                WHERE {where}
                ORDER BY sort_order DESC, created_at DESC
                LIMIT %s OFFSET %s
                """,
                params + (page_size, offset),
            ).fetchall()
            total_row = business.execute(
                f"SELECT count(*) FROM training_materials WHERE {where}", params
            ).fetchone()
            facet_rows = business.execute(
                "SELECT DISTINCT jsonb_array_elements_text(tags) AS tag "
                "FROM training_materials WHERE deleted_at IS NULL AND visible = 1 "
                "ORDER BY tag",
                (),
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [_material_row(row) for row in rows],
        "tag_facet": [str(row["tag"]) for row in facet_rows],
        "page": page,
        "page_size": page_size,
        "total": int(total_row[0]) if total_row else 0,
    }


@router.post("/training-materials/{material_id}/download-url")
def create_training_material_download_url(
    material_id: str, conn: Database, actor: AuthenticatedUser
) -> dict[str, object]:
    """签发短时效防盗链直链（30 分钟），并原子递增下载计数。

    直链为「持有即访问」的公开 URL——过期自动失效；界面在媒体 error 时
    重签一次（方案 §5.2）。签发需登录会话。
    """
    del conn, actor  # 鉴权由依赖完成
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            row = business.execute(
                "SELECT remote_file_id FROM training_materials "
                "WHERE id = %s AND deleted_at IS NULL AND visible = 1",
                (material_id,),
            ).fetchone()
            if row is None:
                raise _http(404, "TRAINING_MATERIAL_NOT_FOUND", "素材不存在或已下架。")
            business.execute(
                "UPDATE training_materials SET download_count = download_count + 1 WHERE id = %s",
                (material_id,),
            )
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    storage, _version = get_training_material_storage()
    try:
        url, expire = storage.resolve_direct_link(
            int(row["remote_file_id"]), ttl_seconds=DOWNLOAD_URL_TTL_SECONDS
        )
    except Pan123FileMissing as exc:
        raise _http(
            404, "TRAINING_MATERIAL_REMOTE_MISSING", "素材文件已不存在，请联系管理员。"
        ) from exc
    except RuntimeError as exc:
        raise _http(
            503, "TRAINING_MATERIAL_STORAGE_FAILED", "素材存储通道暂不可用，请稍后重试。"
        ) from exc
    return {"url": url, "expires_at": str(expire)}


# --- 管理端 -------------------------------------------------------------------


@router.get("/control/training-materials")
def list_training_materials_admin(
    actor: AdminReader,
    include_hidden: bool = True,
    kind: str = "",
    tag: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    del actor
    if kind and kind not in KINDS:
        raise _http(422, "TRAINING_MATERIAL_INVALID_KIND", "素材类型无效。")
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=100)
    clauses = ["m.deleted_at IS NULL"]
    params: list[object] = []
    if not include_hidden:
        clauses.append("m.visible = 1")
    if kind:
        clauses.append("m.kind = %s")
        params.append(kind)
    if tag:
        clauses.append("m.tags @> %s::jsonb")
        params.append(json.dumps([tag], ensure_ascii=False))
    where = " AND ".join(clauses)
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            rows = business.execute(
                f"""
                SELECT m.*, creator.username AS created_by
                FROM training_materials m
                JOIN users creator ON creator.id = m.created_by_user_id
                WHERE {where}
                ORDER BY m.sort_order DESC, m.created_at DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params) + (bounded_limit, bounded_offset),
            ).fetchall()
            total_row = business.execute(
                f"SELECT count(*) FROM training_materials m WHERE {where}", tuple(params)
            ).fetchone()
            facet_rows = business.execute(
                "SELECT DISTINCT jsonb_array_elements_text(tags) AS tag "
                "FROM training_materials WHERE deleted_at IS NULL ORDER BY tag",
                (),
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [_material_row(row) for row in rows],
        "tag_facet": [str(row["tag"]) for row in facet_rows],
        "total": int(total_row[0]) if total_row else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@router.get("/control/training-materials/{material_id}/direct-link")
def get_training_material_direct_link(
    material_id: str, conn: Database, actor: AdminReader
) -> dict[str, object]:
    """管理端复制用直链：不递增下载计数；软删拒绝签发（下架仍可签，便于预演）。"""
    del conn, actor
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            row = business.execute(
                "SELECT remote_file_id, title, file_suffix FROM training_materials "
                "WHERE id = %s AND deleted_at IS NULL",
                (material_id,),
            ).fetchone()
            if row is None:
                raise _http(404, "TRAINING_MATERIAL_NOT_FOUND", "素材不存在或已删除。")
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    storage, _version = get_training_material_storage()
    try:
        url, expire = storage.resolve_direct_link(
            int(row["remote_file_id"]), ttl_seconds=DOWNLOAD_URL_TTL_SECONDS
        )
    except Pan123FileMissing as exc:
        raise _http(404, "TRAINING_MATERIAL_REMOTE_MISSING", "素材文件已不存在。") from exc
    except RuntimeError as exc:
        raise _http(
            503, "TRAINING_MATERIAL_STORAGE_FAILED", "素材存储通道暂不可用，请稍后重试。"
        ) from exc
    return {
        "url": url,
        "expires_at": str(expire),
        "title": str(row["title"]),
        "file_suffix": str(row["file_suffix"]),
    }


class TrainingMaterialCreateContract(AdminWriteRequest):
    """multipart 上传的契约体由服务端在读文件后组装（含内容指纹与规范化
    标签），使同键不同文件/不同标签的重放撞 IDEMPOTENCY_CONFLICT，而不是
    静默覆盖或重复上传。注意契约只含**请求意图**：网盘 fileID 与配置版本
    是每次执行的产物（重放时服务端会重新上传），进契约反而破坏重放。"""

    title: str = ""
    description: str = ""
    tags: list[str] = []
    sort_order: int = 0
    visible: bool = True
    sha256: str = ""
    size_bytes: int = 0
    file_suffix: str = ""
    content_type: str = ""


class TrainingMaterialUpdateRequest(AdminWriteRequest):
    title: str = ""
    description: str = ""
    tags: list[str] | None = None
    sort_order: int | None = None
    visible: bool | None = None


@router.post("/control/training-materials")
async def create_training_material(
    request: Request,
    response: Response,
    actor: ContentWriter,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    tags: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
    visible: Annotated[bool, Form()] = True,
    confirm: Annotated[bool, Form()] = False,
    reason: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    """上传：标签必填（JSON 数组）；远端名含 material_id 前缀全局唯一。"""
    del idempotency_key  # 由 write 契约从请求头读取，这里显式声明只为 OpenAPI 文档
    # 确认与原因缺失时必须在上传副作用前拒绝，不能传完文件才校验。
    require_write_contract(request, AdminWriteRequest(confirm=confirm, reason=reason))
    clean_title = title.strip()
    if not (1 <= len(clean_title) <= MAX_TITLE_CHARS):
        raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "标题必填且不超过 120 字。")
    if len(description.strip()) > MAX_DESCRIPTION_CHARS:
        raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "说明不超过 1000 字。")
    try:
        raw_tags = json.loads(tags) if tags.strip() else []
        if not isinstance(raw_tags, list):
            raise ValueError
    except ValueError as exc:
        raise _http(
            422, "TRAINING_MATERIAL_VALIDATION_FAILED", "标签格式无效，请按要求填写。"
        ) from exc
    normalized_tags = _normalize_tags([str(item) for item in raw_tags])

    suffix = Path(file.filename or "").suffix.lower()
    content_type = (file.content_type or "").lower()
    entry = TRAINING_UPLOADS.get((suffix, content_type))
    if entry is None:
        raise _http(
            415,
            "TRAINING_MATERIAL_TYPE_UNSUPPORTED",
            "仅支持视频（mp4/mov/webm）、音频（mp3/m4a/wav）、图片"
            "（jpg/png/gif/webp）与文档（pdf/docx/doc/xlsx/xls/pptx/ppt）。",
        )
    kind, normalized_suffix = entry

    # 流式落临时文件并计算指纹：大文件不进内存（对齐基础设施审计 R12 纪律）。
    digest = hashlib.sha256()
    total = 0
    with tempfile.NamedTemporaryFile(delete=False, suffix=normalized_suffix) as tmp:
        tmp_path = Path(tmp.name)
        try:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > TRAINING_MATERIAL_MAX_BYTES:
                    raise _http(413, "TRAINING_MATERIAL_TOO_LARGE", "文件超过 200MB 上限。")
                digest.update(chunk)
                tmp.write(chunk)
        except Exception:
            # 超限/中断同样要清理临时文件，不能只在成功路径 finally。
            tmp_path.unlink(missing_ok=True)
            raise
    sha256 = digest.hexdigest()
    material_id = str(uuid.uuid4())
    # 远端名含 material_id 前缀 → 全局唯一（官方上传禁同名；唯一名天然规避
    # duplicate 覆盖/重名歧义）。object_key 保留为逻辑键（展示/日志引用）。
    safe_name = Path(file.filename or "").name or f"material{normalized_suffix}"
    object_key = f"training-materials/{material_id}/{safe_name}"
    remote_name = f"{material_id}-{safe_name}"

    try:
        # 事务 A：解析存储车道并记录配置指纹（业务提交时复核，方案 §3.4）。
        storage, config_version = get_training_material_storage()
        try:
            uploaded = await run_in_threadpool(
                storage.put_file, remote_name, tmp_path, content_type=content_type
            )
        except RuntimeError as exc:
            raise _http(
                503, "TRAINING_MATERIAL_STORAGE_FAILED", "素材上传通道暂不可用，请稍后重试。"
            ) from exc

        def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
            # 统一互斥锁 + 配置指纹复核（方案 §3.4，评审 r3-P1-1）：上传期间配置被
            # 换（换号/换密钥）则拒绝入库，避免留下绑定旧账号的素材行。
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (PAN123_CONFIG_LOCK_KEY,))
            repo = SettingsRepository(BusinessConnection.postgres(conn))
            current_version = repo.provider_config_version(PAN123_PROVIDER_NAME)
            if current_version != config_version:
                raise _http(
                    503,
                    "TRAINING_MATERIAL_CONFIG_CHANGED",
                    "存储配置在本次上传期间发生变化，素材未入库；请重新上传。",
                )
            conn.execute(
                "INSERT INTO training_materials (id, title, description, kind, object_key, "
                "remote_file_id, content_type, file_suffix, size_bytes, sha256, tags, "
                "sort_order, visible, created_by_user_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                (
                    material_id,
                    clean_title,
                    description.strip(),
                    kind,
                    object_key,
                    str(uploaded.file_id),
                    content_type,
                    normalized_suffix,
                    total,
                    sha256,
                    json.dumps(normalized_tags, ensure_ascii=False),
                    sort_order,
                    1 if visible else 0,
                    actor.user_id,
                ),
            )
            conn.execute(
                "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
                "entity_id, metadata_json) VALUES (%s,%s,'training_material.create',"
                "'training_material',%s,%s)",
                (
                    str(uuid.uuid4()),
                    actor.user_id,
                    material_id,
                    json.dumps(
                        {
                            "title": clean_title,
                            "kind": kind,
                            "tags": normalized_tags,
                            "size_bytes": total,
                            "sha256": sha256,
                            "remote_file_id": str(uploaded.file_id),
                            "reason": reason.strip(),
                            "request_id": request_id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            return {"id": material_id, "request_id": request_id}

        try:
            contract = TrainingMaterialCreateContract(
                title=clean_title,
                description=description.strip(),
                tags=normalized_tags,
                sort_order=sort_order,
                visible=visible,
                sha256=sha256,
                size_bytes=total,
                file_suffix=normalized_suffix,
                content_type=content_type,
                confirm=confirm,
                reason=reason,
            )
            return _write_with_idempotency(
                request,
                response,
                actor,
                contract,
                business,
                success_status=201,
                unavailable_code=_UNAVAILABLE_CODE,
                unavailable_message=_UNAVAILABLE_MESSAGE,
            )
        except Exception:
            # 业务失败（含指纹复核拒绝）：网盘侧留下无引用文件（唯一远端名，无污染），
            # 按运维口径记日志人工清理，本期不做自动 GC（方案 §7.5）。
            logger.warning(
                "training material upload not persisted; "
                "orphan remote file file_id=%s material_id=%s",
                uploaded.file_id,
                material_id,
            )
            raise
    finally:
        tmp_path.unlink(missing_ok=True)


@router.patch("/control/training-materials/{material_id}")
def update_training_material(
    material_id: str,
    body: TrainingMaterialUpdateRequest,
    request: Request,
    response: Response,
    actor: ContentWriter,
) -> dict[str, object]:
    """部分更新：非空字段覆盖；tags/sort_order/visible 用 None 表示不修改。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT id FROM training_materials WHERE id=%s AND deleted_at IS NULL",
            (material_id,),
        ).fetchone()
        if row is None:
            raise _http(404, "TRAINING_MATERIAL_NOT_FOUND", "素材不存在或已删除。")
        sets: list[str] = []
        params: list[object] = []
        if body.title.strip():
            title = body.title.strip()
            if len(title) > MAX_TITLE_CHARS:
                raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "标题不超过 120 字。")
            sets.append("title=%s")
            params.append(title)
        if body.description.strip():
            description = body.description.strip()
            if len(description) > MAX_DESCRIPTION_CHARS:
                raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "说明不超过 1000 字。")
            sets.append("description=%s")
            params.append(description)
        if body.tags is not None:
            normalized = _normalize_tags(body.tags)
            sets.append("tags=%s::jsonb")
            params.append(json.dumps(normalized, ensure_ascii=False))
        if body.sort_order is not None:
            sets.append("sort_order=%s")
            params.append(body.sort_order)
        if body.visible is not None:
            sets.append("visible=%s")
            params.append(1 if body.visible else 0)
        if not sets:
            raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "没有可更新的字段。")
        sets.append("updated_at=now()")
        params.append(material_id)
        conn.execute(
            f"UPDATE training_materials SET {', '.join(sets)} WHERE id=%s",  # noqa: S608
            tuple(params),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'training_material.update',"
            "'training_material',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                material_id,
                json.dumps(
                    {"fields": sets, "reason": body.reason.strip(), "request_id": request_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": material_id, "request_id": request_id}

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )


@router.delete("/control/training-materials/{material_id}")
def delete_training_material(
    material_id: str,
    request: Request,
    response: Response,
    actor: ContentWriter,
    body: AdminWriteRequest,
) -> dict[str, object]:
    """软删：客户立即不可见且无法重签直链；网盘文件保留（误删可恢复）。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        result = conn.execute(
            "UPDATE training_materials SET deleted_at=now(), updated_at=now() "
            "WHERE id=%s AND deleted_at IS NULL RETURNING id",
            (material_id,),
        ).fetchone()
        if result is None:
            raise _http(404, "TRAINING_MATERIAL_NOT_FOUND", "素材不存在或已删除。")
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'training_material.delete',"
            "'training_material',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                material_id,
                json.dumps(
                    {"reason": body.reason.strip(), "request_id": request_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": material_id, "deleted": True, "request_id": request_id}

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )
