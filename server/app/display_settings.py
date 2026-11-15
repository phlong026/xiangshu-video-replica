"""客户端展示配置（键值）读取路由与存储助手。

管理端写路径在本模块之外（settings_routes 的 /display/{key}，走管理端
鉴权与审计）；客户端经白名单键只读。首键 ``prompt_tutorial_url``：参考
生视频页「提示词教程」外链——写死在前端常量时每换一次链接就要发一次
版，搬进数据库后运营改配置即时生效。
"""

from __future__ import annotations

import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from app.auth import ShortAuthUser
from app.db_portable import BusinessConnection

router = APIRouter(prefix="/api/studio", tags=["display-settings"])

PUBLIC_KEYS: tuple[str, ...] = ("prompt_tutorial_url",)


def read_display_setting(conn: BusinessConnection, key: str) -> str:
    row = conn.execute(
        "SELECT value FROM display_settings WHERE key = %s",
        (key,),
    ).fetchone()
    return str(row["value"]) if row is not None else ""


def save_display_setting(
    conn: BusinessConnection,
    *,
    key: str,
    value: str,
    actor_user_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO display_settings (key, value, updated_by_user_id, updated_at)
        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (key) DO UPDATE SET
            value = excluded.value,
            updated_by_user_id = excluded.updated_by_user_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (key, value, actor_user_id or None),
    )


def write_display_audit(conn: BusinessConnection, *, key: str, actor_user_id: str) -> None:
    conn.execute(
        """INSERT INTO audit_logs(id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES(%s, %s, 'display_settings.update', 'display_settings', %s, %s)""",
        (
            str(uuid4()),
            actor_user_id or None,
            key,
            json.dumps({"key": key}, ensure_ascii=False),
        ),
    )


@router.get("/display-settings/{key}")
def read_public_display_setting(key: str, actor: ShortAuthUser) -> dict[str, str]:
    """客户只读白名单键；非白名单键一律 404，不暴露管理端配置面。"""
    del actor
    if key not in PUBLIC_KEYS:
        raise HTTPException(
            status_code=404,
            detail={"code": "DISPLAY_SETTING_NOT_FOUND", "message": "配置项不存在。"},
        )
    from app.db_pg import pg_transaction

    with pg_transaction() as raw:
        value = read_display_setting(BusinessConnection.postgres(raw), key)
    return {"key": key, "value": value}
