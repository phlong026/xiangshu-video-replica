"""ADM-18 发布效果回流（默认关闭，授权口径待 owner 确认）。

三个端点：

- ``GET /api/control/publish-stats``：已发布记录清单（客户/平台/标题/短链/
  已存 stats/同步时间），按发布时间倒序分页——"爆款赋能效果"的数据面。
- ``PUT /api/control/publish-stats/enabled``：总开关（默认关；审计
  ``publish.stats.toggle``）。开启即代表运营确认了对客户发布内容的采集
  授权口径。
- ``POST /api/control/publish-stats/{id}/sync``：按记录同步——抖音经
  ``douyin_refresh``（ID 当关键词搜索后精确认领，计平台成本 viral_data），
  命中即把 likes/comments/shares/collects 写入 ``stats`` jsonb 并盖
  ``stats_synced_at``；视频号暂不支持（上游无稳定单条详情通道）。

设计边界：同步走平台成本（不计客户积分）；失败不落半截数据；重复同步
直接覆盖 stats（累计值语义）。
"""

from __future__ import annotations

import json
import logging
import uuid

import psycopg
from fastapi import APIRouter, Request, Response
from fastapi import HTTPException as _HTTPError

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_customer_routes import _write_with_idempotency
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.usage_billing import accept_platform_operation, finish_source
from app.viral_tikhub import viral_source_client_from_settings

router = APIRouter(prefix="/api/control", tags=["admin-publish-stats"])
logger = logging.getLogger(__name__)

PAGE_LIMIT = 20
_UNAVAILABLE_CODE = "PUBLISH_STATS_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "发布效果回流需要 PostgreSQL 运行时。"


def _flag(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT publish_stats_sync_enabled FROM runtime_settings WHERE id=1"
    ).fetchone()
    return bool(row and int(row[0]) == 1)


@router.get("/publish-stats/enabled")
def get_publish_stats_enabled(actor: AdminReader) -> dict[str, bool]:
    """菜单显隐只需要开关状态：单独一个轻接口，避免为此拉一整页列表。"""
    del actor
    try:
        with pg_transaction() as conn:
            return {"enabled": _flag(conn)}
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc


@router.get("/publish-stats")
def list_publish_stats(
    actor: AdminReader,
    username: str = "",
    limit: int = PAGE_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    del actor
    bounded_limit = max(1, min(int(limit), 100))
    bounded_offset = max(0, int(offset))
    clauses = ["pr.status = 'published'"]
    params: list[object] = []
    if username.strip():
        clauses.append("u.username ILIKE %s")
        params.append(f"%{username.strip()}%")
    where = " AND ".join(clauses)
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT pr.id, u.username, COALESCE(u.display_name, ''),
                       pr.platform, pr.title, pr.platform_item_id,
                       pr.platform_short_url, pr.stats, pr.stats_synced_at,
                       pr.published_at, pr.created_at
                FROM publish_records pr
                JOIN users u ON u.id = pr.user_id
                WHERE {where}
                ORDER BY pr.published_at DESC NULLS LAST, pr.created_at DESC
                LIMIT %s OFFSET %s
                """,  # noqa: S608
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            total = conn.execute(
                f"""
                SELECT count(*) FROM publish_records pr
                JOIN users u ON u.id = pr.user_id WHERE {where}
                """,  # noqa: S608
                params,
            ).fetchone()
            enabled = _flag(conn)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "enabled": enabled,
        "items": [
            {
                "id": str(row[0]),
                "username": str(row[1]),
                "display_name": str(row[2]),
                "platform": str(row[3]),
                "title": str(row[4] or ""),
                "platform_item_id": str(row[5] or ""),
                "short_url": str(row[6] or ""),
                "stats": row[7] if row[7] is not None else None,
                "stats_synced_at": row[8].isoformat() if row[8] else "",
                "published_at": row[9].isoformat() if row[9] else "",
            }
            for row in rows
        ],
        "total": int(total[0]) if total else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "rule": (
            "同步已发布内容的公开互动数据（平台累计值）。默认关闭：开启前需确认"
            "对客户发布内容的采集授权口径。仅抖音支持单条同步；费用记平台成本，"
            "不扣客户积分。"
        ),
    }


class PublishStatsToggleRequest(AdminWriteRequest):
    enabled: bool = False


@router.put("/publish-stats/enabled")
def toggle_publish_stats(
    body: PublishStatsToggleRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn.execute(
            "INSERT INTO runtime_settings (id, publish_stats_sync_enabled) "
            "VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET "
            "publish_stats_sync_enabled = EXCLUDED.publish_stats_sync_enabled",
            (1 if body.enabled else 0,),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'publish.stats.toggle',"
            "'runtime_settings','publish_stats',%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "enabled": body.enabled,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"enabled": body.enabled, "request_id": request_id}

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


@router.post("/publish-stats/{record_id}/sync")
def sync_publish_stats(
    record_id: str,
    request: Request,
    response: Response,
    actor: AdminWriter,
    body: AdminWriteRequest,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if not _flag(conn):
            raise _http(
                409,
                "PUBLISH_STATS_SYNC_DISABLED",
                "发布效果回流未开启：开启前需确认客户发布内容的采集授权口径。",
            )
        row = conn.execute(
            "SELECT platform, platform_item_id FROM publish_records "
            "WHERE id=%s AND status='published'",
            (record_id,),
        ).fetchone()
        if row is None:
            raise _http(404, "PUBLISH_RECORD_NOT_FOUND", "发布记录不存在或未发布。")
        platform, item_id = str(row[0]), str(row[1] or "")
        if platform != "douyin":
            raise _http(
                422,
                "PUBLISH_STATS_PLATFORM_UNSUPPORTED",
                "暂仅支持抖音发布记录的效果同步。",
            )
        if not item_id:
            raise _http(422, "PUBLISH_STATS_ITEM_MISSING", "该记录缺少平台内容编号。")
        source_id = f"publish_stats:{record_id}"
        operation_id = accept_platform_operation(
            BusinessConnection.postgres(conn),
            service="viral_data",
            source_id=source_id,
            api_metadata={"kind": "publish_stats_sync", "platform": platform},
        )
        client = viral_source_client_from_settings(BusinessConnection.postgres(conn))
        try:
            videos = client.douyin_refresh(platform="douyin", video_id=item_id)
            matched = next((video for video in videos if video.video_id == item_id), None)
            if matched is None:
                raise _http(
                    503,
                    "PUBLISH_STATS_UPSTREAM_MISS",
                    "上游未返回该条内容的数据，未记账成本请稍后重试。",
                )
        except _HTTPError:
            raise
        except Exception as exc:
            logger.warning("publish stats sync failed record=%s: %s", record_id, exc)
            finish_source(
                BusinessConnection.postgres(conn),
                source_id,
                units=0,
                succeeded=False,
                service="viral_data",
            )
            raise _http(
                503, "PUBLISH_STATS_UPSTREAM_FAILED", "上游数据服务异常，请稍后重试。"
            ) from exc
        finish_source(
            BusinessConnection.postgres(conn),
            source_id,
            units=1,
            succeeded=True,
            service="viral_data",
        )
        stats = {
            "likes": matched.likes,
            "comments": matched.comments,
            "shares": matched.shares,
            "collects": matched.collects,
        }
        conn.execute(
            "UPDATE publish_records SET stats=%s::jsonb, stats_synced_at=now(), "
            "sync_requested=0, updated_at=now() WHERE id=%s",
            (json.dumps(stats, ensure_ascii=False), record_id),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'publish.stats.sync',"
            "'publish_record',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                record_id,
                json.dumps(
                    {"stats": stats, "operation_id": operation_id, "request_id": request_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": record_id, "stats": stats, "request_id": request_id}

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
