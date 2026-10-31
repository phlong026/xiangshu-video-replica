"""平台培训素材库（IA 改版第 5 项）：后台统一上传，客户只读。

素材库页面从「用户自传素材」整体替换为本模块的数据源（产品已拍板彻底
替换，2026-11-01）。用户自传链路（参考视频 / 人物图 / 发布封面）不走
本表，仍由 ``materials``/``media`` 域承载。

两组端点、同一张表（与公告同构的「admin 管理 + 客户只读」范式）：

- **管理端** ``/api/control/training-materials``：multipart 上传（视频/
  文档白名单）、改标题/说明/排序/上下架、软删。写走四段契约（真实
  管理员会话、Idempotency-Key、confirm、reason），审计 ``training_material.*``。
- **客户侧** ``/api/training-materials``：上架素材分页列表（kind 筛选
  视频/培训文档）；``POST /{id}/download-url`` 签发短时效下载链接——
  云端存储返回预签名 URL，本地车道返回 HMAC 签名（绑定 user/session
  epoch）的站内 URL，由 ``GET /{id}/file`` 逐次复验会话与可见性后放行，
  踢线（epoch 递增）后旧链接立即失效。

``kind`` 由上传白名单从文件类型推导，不接受手填，避免「视频栏目里出现
PDF」这类栏目错位。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlencode

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
from app.auth import AuthenticatedUser, Database
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.media_routes import (
    api_base_url,
    get_media_storage,
    read_stored_object,
    signed_asset_session_epoch,
)
from app.settings import settings_encryption_key
from app.sql_pagination import page_bounds
from app.storage import StorageBackendUnavailable, local_download_signature

router = APIRouter(prefix="/api", tags=["training-materials"])
logger = logging.getLogger(__name__)

KINDS = ("video", "training")
_KIND_LABELS = {"video": "视频", "training": "培训文档"}
MAX_TITLE_CHARS = 120
MAX_DESCRIPTION_CHARS = 1000

# 「后缀 + content-type 成对登记」防混搭（与 materials.ALLOWED_UPLOADS 同一纪律）。
# 值为 (kind, 归一化后缀)：kind 由文件类型推导，不接受管理员手填。
TRAINING_UPLOADS: dict[tuple[str, str], tuple[str, str]] = {
    (".mp4", "video/mp4"): ("video", ".mp4"),
    (".mov", "video/quicktime"): ("video", ".mov"),
    (".pdf", "application/pdf"): ("training", ".pdf"),
    (
        ".docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ): ("training", ".docx"),
    (
        ".pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ): ("training", ".pptx"),
    (
        ".xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ): ("training", ".xlsx"),
}
TRAINING_MATERIAL_MAX_BYTES = 200 * 1024 * 1024  # 与媒体车道 MAX_UPLOAD_BYTES 对齐
DOWNLOAD_URL_EXPIRES_IN = timedelta(minutes=15)
_UPLOAD_CHUNK_BYTES = 1024 * 1024

_UNAVAILABLE_CODE = "TRAINING_MATERIAL_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "培训素材库需要 PostgreSQL 运行时。"


def _ts(value: object) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _material_row(row: Any) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "title": str(row["title"]),
        "description": str(row["description"] or ""),
        "kind": str(row["kind"]),
        "kind_label": _KIND_LABELS.get(str(row["kind"]), str(row["kind"])),
        "content_type": str(row["content_type"]),
        "file_suffix": str(row["file_suffix"]),
        "size_bytes": int(row["size_bytes"]),
        "sort_order": int(row["sort_order"]),
        "visible": bool(row["visible"]),
        "download_count": int(row["download_count"]),
        # _NamedRow 无 .get()；客户列表 SELECT * 不带创建者列，缺列回退空串。
        "created_by": str(getattr(row, "created_by", "") or ""),
        "created_at": _ts(row["created_at"]),
        "updated_at": _ts(row["updated_at"]),
    }


# --- 客户侧 -------------------------------------------------------------------


@router.get("/training-materials")
def list_training_materials(
    conn: Database,
    actor: AuthenticatedUser,
    kind: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 24,
) -> dict[str, object]:
    """上架素材（置顶序优先，新→旧次之）。只读，客户无任何写入口。"""
    del conn, actor  # 鉴权由依赖完成；列表查询只依赖数据库时钟
    if kind and kind not in KINDS:
        raise _http(422, "TRAINING_MATERIAL_INVALID_KIND", "素材类型无效。")
    kind_clause = "AND kind = %s" if kind else ""
    kind_params: tuple[object, ...] = (kind,) if kind else ()
    offset = (page - 1) * page_size
    try:
        with pg_transaction() as pg:
            # 命名行访问统一经 BusinessConnection（裸 psycopg 连接是 tuple 行）。
            business = BusinessConnection.postgres(pg)
            rows = business.execute(
                f"""
                SELECT * FROM training_materials
                WHERE deleted_at IS NULL AND visible = 1 {kind_clause}
                ORDER BY sort_order DESC, created_at DESC
                LIMIT %s OFFSET %s
                """,
                kind_params + (page_size, offset),
            ).fetchall()
            total_row = business.execute(
                f"SELECT count(*) FROM training_materials "
                f"WHERE deleted_at IS NULL AND visible = 1 {kind_clause}",
                kind_params,
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [_material_row(row) for row in rows],
        "page": page,
        "page_size": page_size,
        "total": int(total_row[0]) if total_row else 0,
    }


@router.post("/training-materials/{material_id}/download-url")
def create_training_material_download_url(
    material_id: str, conn: Database, actor: AuthenticatedUser
) -> dict[str, object]:
    """签发短时效下载链接；云端为预签名 URL，本地车道为 HMAC 签名站内 URL。"""
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            row = business.execute(
                "SELECT * FROM training_materials "
                "WHERE id = %s AND deleted_at IS NULL AND visible = 1",
                (material_id,),
            ).fetchone()
            if row is None:
                raise _http(404, "TRAINING_MATERIAL_NOT_FOUND", "素材不存在或已下架。")
            business.execute(
                "UPDATE training_materials SET download_count = download_count + 1 WHERE id = %s",
                (material_id,),
            )
            storage = get_media_storage(business)
            object_key = str(row["object_key"])
            provider = storage.provider
            epoch = signed_asset_session_epoch(business, actor)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    except StorageBackendUnavailable as exc:
        raise _http(503, "STORAGE_PROVIDER_UNAVAILABLE", "对象存储暂不可用。") from exc

    expires_at = str(int(time.time()) + int(DOWNLOAD_URL_EXPIRES_IN.total_seconds()))
    if provider != "local":
        # 云端车道：预签名 URL 即最终地址（<video>/<a> 无法附带鉴权头）。
        intent = storage.create_download_intent(
            object_key, expires_in=DOWNLOAD_URL_EXPIRES_IN, can_read=True
        )
        return {"url": intent.url, "expires_at": intent.expires_at}
    query = {
        "expires": expires_at,
        "user_id": actor.id,
        "session_epoch": epoch,
    }
    query["sig"] = local_download_signature(
        object_key,
        expires_at,
        user_id=actor.id,
        asset_id=material_id,
        session_epoch=epoch,
        secret=settings_encryption_key(),
    )
    url = (
        f"{api_base_url()}/api/training-materials/{quote(material_id, safe='')}"
        f"/file?{urlencode(query)}"
    )
    return {"url": url, "expires_at": expires_at}


@router.get("/training-materials/{material_id}/file")
def get_training_material_file(
    material_id: str,
    request: Request,
    expires: str = "",
    user_id: str = "",
    session_epoch: str = "",
    sig: str = "",
) -> Response:
    """本地车道签名下载（带 Range，供页内播放拖动）。

    与资产签名通道同纪律：签名短时效且绑定 user/session epoch，读取时
    逐次复验（素材仍上架、会话未被踢线），踢线后旧链接立即失效。
    """
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            row = business.execute(
                "SELECT object_key FROM training_materials "
                "WHERE id = %s AND deleted_at IS NULL AND visible = 1",
                (material_id,),
            ).fetchone()
            if row is None:
                raise _http(404, "TRAINING_MATERIAL_NOT_FOUND", "素材不存在或已下架。")
            object_key = str(row["object_key"])
            # epoch=0 表示内部身份签发；客户链接必须核验会话仍在租约内。
            if session_epoch != "0":
                session_id, _, epoch_value = session_epoch.partition(":")
                alive = business.execute(
                    "SELECT 1 FROM customer_session_state "
                    "WHERE session_id = %s AND session_epoch = %s "
                    "AND lease_until::timestamptz > clock_timestamp()",
                    (session_id, epoch_value),
                ).fetchone()
                if alive is None:
                    raise _http(
                        401, "TRAINING_MATERIAL_SESSION_EXPIRED", "登录状态已变化，请刷新后重试。"
                    )
            storage = get_media_storage(BusinessConnection.postgres(pg))
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    except StorageBackendUnavailable as exc:
        raise _http(503, "STORAGE_PROVIDER_UNAVAILABLE", "对象存储暂不可用。") from exc

    if not expires.isdigit() or int(expires) < int(time.time()):
        raise _http(410, "TRAINING_MATERIAL_URL_EXPIRED", "下载链接已过期，请重新获取。")
    expected = local_download_signature(
        object_key,
        expires,
        user_id=user_id,
        asset_id=material_id,
        session_epoch=session_epoch,
        secret=settings_encryption_key(),
    )
    if not hmac.compare_digest(expected, sig):
        raise _http(403, "TRAINING_MATERIAL_SIGNATURE_INVALID", "下载链接无效。")
    if storage.provider != "local":
        # 云端车道的下载走预签名 URL；此端点只服务本地车道。
        raise _http(404, "TRAINING_MATERIAL_LOCAL_UNAVAILABLE", "该素材不通过本端点下载。")
    return read_stored_object(
        storage,
        object_key=object_key,
        range_header=request.headers.get("range"),
        disposition="inline",
    )


# --- 管理端 -------------------------------------------------------------------


@router.get("/control/training-materials")
def list_training_materials_admin(
    actor: AdminReader,
    include_hidden: bool = True,
    kind: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    del actor
    if kind and kind not in KINDS:
        raise _http(422, "TRAINING_MATERIAL_INVALID_KIND", "素材类型无效。")
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=100)
    kind_clause = "AND kind = %s" if kind else ""
    kind_params: tuple[object, ...] = (kind,) if kind else ()
    hidden_clause = "" if include_hidden else "AND visible = 1"
    try:
        with pg_transaction() as pg:
            business = BusinessConnection.postgres(pg)
            rows = business.execute(
                f"""
                SELECT m.*, creator.username AS created_by
                FROM training_materials m
                JOIN users creator ON creator.id = m.created_by_user_id
                WHERE m.deleted_at IS NULL {hidden_clause} {kind_clause}
                ORDER BY m.sort_order DESC, m.created_at DESC
                LIMIT %s OFFSET %s
                """,
                kind_params + (bounded_limit, bounded_offset),
            ).fetchall()
            total_row = business.execute(
                f"SELECT count(*) FROM training_materials m "
                f"WHERE m.deleted_at IS NULL {hidden_clause} {kind_clause}",
                kind_params,
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [_material_row(row) for row in rows],
        "total": int(total_row[0]) if total_row else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


class TrainingMaterialCreateContract(AdminWriteRequest):
    """multipart 上传的契约体由服务端在读文件后组装（含内容指纹），
    使同键不同文件的重放撞 IDEMPOTENCY_CONFLICT 而不是静默覆盖。"""

    title: str = ""
    description: str = ""
    sort_order: int = 0
    visible: bool = True
    sha256: str = ""
    size_bytes: int = 0
    file_suffix: str = ""
    content_type: str = ""


class TrainingMaterialUpdateRequest(AdminWriteRequest):
    title: str = ""
    description: str = ""
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
    sort_order: Annotated[int, Form()] = 0,
    visible: Annotated[bool, Form()] = True,
    confirm: Annotated[bool, Form()] = False,
    reason: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    del idempotency_key  # 由 write 契约从请求头读取，这里显式声明只为 OpenAPI 文档
    clean_title = title.strip()
    if not (1 <= len(clean_title) <= MAX_TITLE_CHARS):
        raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "标题必填且不超过 120 字。")
    if len(description.strip()) > MAX_DESCRIPTION_CHARS:
        raise _http(422, "TRAINING_MATERIAL_VALIDATION_FAILED", "说明不超过 1000 字。")
    suffix = Path(file.filename or "").suffix.lower()
    content_type = (file.content_type or "").lower()
    entry = TRAINING_UPLOADS.get((suffix, content_type))
    if entry is None:
        raise _http(
            415,
            "TRAINING_MATERIAL_TYPE_UNSUPPORTED",
            "仅支持 mp4/mov 视频与 pdf/docx/pptx/xlsx 文档。",
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
    safe_name = Path(file.filename or "").name or f"material{normalized_suffix}"
    # 内容寻址键：同内容重传复用同一对象，孤儿对象也无害（无行引用）。
    object_key = f"training-materials/{material_id}/{sha256}/{safe_name}"

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute(
            "INSERT INTO training_materials (id, title, description, kind, object_key, "
            "content_type, file_suffix, size_bytes, sha256, sort_order, visible, "
            "created_by_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                material_id,
                clean_title,
                description.strip(),
                kind,
                object_key,
                content_type,
                normalized_suffix,
                total,
                sha256,
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
                        "size_bytes": total,
                        "sha256": sha256,
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
        try:
            # 存储解析与对象写入在短作用域内完成，不与业务事务互相持有连接。
            with pg_transaction() as pg:
                storage = get_media_storage(BusinessConnection.postgres(pg))
        except (RuntimeError, MissingDatabaseConfigError) as exc:
            raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
        try:
            await run_in_threadpool(
                storage.put_file, object_key, tmp_path, content_type=content_type
            )
        except StorageBackendUnavailable as exc:
            raise _http(503, "STORAGE_PROVIDER_UNAVAILABLE", "对象存储暂不可用。") from exc
        # 契约体带内容指纹：重放校验把「同键不同文件」拦成 409。
        contract = TrainingMaterialCreateContract(
            title=clean_title,
            description=description.strip(),
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
    finally:
        # 业务失败会留下无行引用的孤儿对象（内容寻址键，无污染）；临时文件必清。
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("training material temp file cleanup failed: %s", tmp_path)


@router.patch("/control/training-materials/{material_id}")
def update_training_material(
    material_id: str,
    body: TrainingMaterialUpdateRequest,
    request: Request,
    response: Response,
    actor: ContentWriter,
) -> dict[str, object]:
    """部分更新：非空字段覆盖；sort_order/visible 用 None 表示不修改。"""

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
    """软删：客户立即不可见；对象留在存储中，误删可通过追加恢复行挽回。"""

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
