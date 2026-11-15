"""C5 publish routes (phase 1): platform account authorization for the studio.

Read routes ride the plain authenticated lane; every write goes through the
T21 fenced ``BusinessDbDep.write()`` exactly like the other studio routes.

Only the four legacy cookie-paste ``/accounts`` endpoints live here: list,
connect, unbind and request an async login-state probe. The delivery path
(``/api/studio/publish/records``) is ``app.publish_record_routes`` and reads
the browser-login accounts (``/api/studio/publish/browser/accounts``).

No endpoint ever returns credential material — the response models in
app.publish carry display_name / status / timestamps only.
"""

from __future__ import annotations

import base64
import time
from typing import Any, Literal

from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth import ReadDatabase, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.hifly import HiflyError, hifly_client_from_settings
from app.publish import (
    PublishAccountCreateRequest,
    PublishAccountDeleteResponse,
    PublishAccountListResponse,
    PublishAccountResponse,
    PublishVerifyResponse,
    create_account,
    delete_account,
    list_accounts,
    request_account_verify,
)
from app.settings import fernet_from_environment

router = APIRouter(prefix="/api/studio/publish")


def _fernet() -> Fernet:
    return fernet_from_environment()


@router.get("/accounts", response_model=PublishAccountListResponse)
def read_publish_accounts(
    conn: ReadDatabase,
    actor: ShortAuthUser,
) -> PublishAccountListResponse:
    return list_accounts(conn, actor_id=actor.id)


@router.post("/accounts", response_model=PublishAccountResponse)
def create_publish_account(
    request: PublishAccountCreateRequest,
    db: BusinessDbDep,
) -> PublishAccountResponse:
    with db.write() as (conn, actor):
        return create_account(conn, actor=actor, fernet=_fernet(), request=request)


@router.delete("/accounts/{account_id}", response_model=PublishAccountDeleteResponse)
def delete_publish_account(
    account_id: str,
    db: BusinessDbDep,
) -> PublishAccountDeleteResponse:
    with db.write() as (conn, actor):
        delete_account(conn, actor_id=actor.id, account_id=account_id)
    return PublishAccountDeleteResponse(deleted=True)


@router.post("/accounts/{account_id}/verify", response_model=PublishVerifyResponse)
def verify_publish_account(
    account_id: str,
    db: BusinessDbDep,
) -> PublishVerifyResponse:
    with db.write() as (conn, actor):
        request_account_verify(conn, actor_id=actor.id, account_id=account_id)
    return PublishVerifyResponse(submitted=True)


# --- 封面模板（飞影，2026-11-15 实测打通） ------------------------------------
#
# 产品流程（用户拍板）：发布草稿「封面选择」新增「模板封面」来源——拉模板 →
# 用文案自动生成封面 → 转存为素材 → 作为发布封面。生成是同步接口、实测
# 60-90 秒返回，因此路由放宽超时、前端用 180s 请求 + 显式加载态；量级上来
# 后再升级为任务表 + 轮询（先简单，不做半吊子跨副本状态）。

_COVER_STYLES_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_COVER_STYLES_TTL_SECONDS = 3600.0


def _hifly_client() -> Any:
    with pg_transaction() as raw:
        return hifly_client_from_settings(BusinessConnection.postgres(raw))


@router.get("/cover-styles")
def list_hifly_cover_styles(
    conn: ReadDatabase,
    actor: ShortAuthUser,
) -> dict[str, object]:
    """可用封面模板（进程内缓存 1 小时；模板列表基本静态）。"""
    del conn
    del actor
    now = time.monotonic()
    cached = _COVER_STYLES_CACHE.get("styles")
    if cached is not None and now - cached[0] < _COVER_STYLES_TTL_SECONDS:
        return cached[1]
    try:
        payload = _hifly_client().cover_styles(page=1, size=200)
    except HiflyError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "COVER_STYLES_UNAVAILABLE", "message": str(exc)},
        ) from exc
    items = [
        {
            "id": str(style.get("id") or ""),
            "title": str(style.get("title") or ""),
            "url": str(style.get("url") or ""),
        }
        for style in payload.get("data") or []
        if style.get("id")
    ]
    response: dict[str, object] = {"items": items, "total": len(items)}
    _COVER_STYLES_CACHE["styles"] = (now, response)
    return response


class CoverGenerateRequest(BaseModel):
    template: str
    content: str
    # 数字人标识可选：把封面中的形象替换成用户自己的分身。
    avatar: str | None = None
    canvas_preset: Literal["9:16", "3:4"] = "9:16"
    output_format: Literal["jpg", "png"] = "jpg"


@router.post("/cover-generate")
def generate_hifly_cover(
    request: CoverGenerateRequest,
    conn: ReadDatabase,
    actor: ShortAuthUser,
) -> dict[str, object]:
    """按模板生成封面（同步，实测 60-90s）并以 base64 内联返回图片字节。

    直接回飞影 CDN URL 会踩时效与浏览器 CORS 两个坑：内联字节让前端走既有
    素材上传管线落自有存储，发布草稿的 cover_id 语义完全不变。
    """
    del conn
    del actor
    content = request.content.strip()
    if not content:
        raise HTTPException(
            status_code=422,
            detail={"code": "COVER_CONTENT_REQUIRED", "message": "文案内容不能为空。"},
        )
    try:
        payload = _hifly_client().create_cover(
            template=request.template,
            content=content,
            avatar=request.avatar,
            canvas_preset=request.canvas_preset,
            output_format=request.output_format,
        )
    except HiflyError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "COVER_GENERATION_FAILED", "message": str(exc)},
        ) from exc
    url = str(payload.get("url") or "")
    if not url:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "COVER_GENERATION_NO_URL",
                "message": "封面已生成但未返回图片地址，请重试。",
            },
        )
    import urllib.request

    with urllib.request.urlopen(url, timeout=60) as image_response:
        image_bytes = image_response.read()
    return {
        "cover_id": str(payload.get("cover_id") or ""),
        "url": url,
        "image_base64": base64.b64encode(image_bytes).decode("ascii"),
        "output_format": request.output_format,
    }
