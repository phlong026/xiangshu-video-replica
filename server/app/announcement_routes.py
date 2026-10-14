"""M2 公告与消息中心（ADM-09）。

两组端点、同一张表：

- **管理端** ``/api/control/announcements``：CRUD（写走四段契约：真实管理员
  会话、Idempotency-Key、confirm、reason；审计 ``announcement.*``）。列表带
  每条公告的去重已读人数，评估触达效果。
- **客户侧** ``/api/announcements``：当前生效集合（置顶优先，最多 10 条），
  客户端进入工作台拉一次并展示；``POST /{id}/read`` 幂等回执（重复读不重复
  计数）。

色调只有三档：info（常规）/ maintenance（维护公告，客户端黄色横幅）/
promo（活动，金色横幅）。区间默认立即生效、永久有效。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, Request, Response

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_customer_routes import _write_with_idempotency
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.auth import AuthenticatedUser, Database
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.sql_pagination import page_bounds

router = APIRouter(prefix="/api", tags=["announcements"])
logger = logging.getLogger(__name__)

TONES = ("info", "maintenance", "promo")
_TONE_LABELS = {"info": "常规", "maintenance": "维护", "promo": "活动"}
MAX_TITLE_CHARS = 120
MAX_BODY_CHARS = 4000
CUSTOMER_FEED_LIMIT = 10

_UNAVAILABLE_CODE = "ANNOUNCEMENT_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "公告功能需要 PostgreSQL 运行时。"


def _parse_ts(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", f"{field} 时间格式无效。") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class AnnouncementCreateRequest(AdminWriteRequest):
    title: str = ""
    body: str = ""
    tone: str = "info"
    starts_at: str = ""
    ends_at: str = ""
    pinned: bool = False


class AnnouncementUpdateRequest(AdminWriteRequest):
    title: str = ""
    body: str = ""
    tone: str = ""
    starts_at: str = ""
    ends_at: str = ""
    pinned: bool | None = None
    # 显式清空失效时间（改回「不限」）；ends_at 留空只表示「不修改」。
    clear_ends_at: bool = False


def _live_clause(alias: str = "a") -> str:
    return (
        f"({alias}.deleted_at IS NULL AND {alias}.starts_at<=now() "
        f"AND ({alias}.ends_at IS NULL OR {alias}.ends_at>now()))"
    )


def _ts(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _announcement_row(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "id": str(row[0]),
        "title": str(row[1]),
        "body": str(row[2]),
        "tone": str(row[3]),
        "tone_label": _TONE_LABELS.get(str(row[3]), str(row[3])),
        "starts_at": _ts(row[4]),
        "ends_at": _ts(row[5]),
        "pinned": bool(row[6]),
        "created_by": str(row[7]),
        "created_at": _ts(row[8]),
        "read_count": int(row[9]),  # type: ignore[call-overload]
        "live": bool(row[10]),
    }


# --- 客户侧 -------------------------------------------------------------------


@router.get("/announcements")
def list_live_announcements(conn: Database, actor: AuthenticatedUser) -> dict[str, object]:
    """当前生效公告（置顶优先，最新次之，≤10 条），附带当前用户已读标记。"""
    try:
        with pg_transaction() as pg:
            rows = pg.execute(
                f"""
                SELECT a.id, a.title, a.body, a.tone, a.starts_at, a.ends_at,
                       a.pinned, creator.username, a.created_at,
                       (SELECT count(*) FROM announcement_reads r
                         WHERE r.announcement_id = a.id),
                       TRUE,
                       EXISTS (SELECT 1 FROM announcement_reads mine
                                WHERE mine.announcement_id = a.id
                                  AND mine.user_id = %s)
                FROM announcements a
                JOIN users creator ON creator.id = a.created_by_user_id
                WHERE {_live_clause()}
                ORDER BY a.pinned DESC, a.created_at DESC
                LIMIT %s
                """,
                (actor.id, CUSTOMER_FEED_LIMIT),
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    items = [_announcement_row(row) | {"read": bool(row[11])} for row in rows]
    return {"items": items}


@router.post("/announcements/{announcement_id}/read")
def mark_announcement_read(
    announcement_id: str, conn: Database, actor: AuthenticatedUser
) -> dict[str, object]:
    """幂等已读回执：重复提交不产生第二行，也不报错。"""
    try:
        with pg_transaction() as pg:
            exists = pg.execute(
                "SELECT 1 FROM announcements WHERE id=%s AND deleted_at IS NULL",
                (announcement_id,),
            ).fetchone()
            if exists is None:
                raise _http(404, "ANNOUNCEMENT_NOT_FOUND", "公告不存在或已下线。")
            pg.execute(
                "INSERT INTO announcement_reads (announcement_id, user_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (announcement_id, actor.id),
            )
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {"announcement_id": announcement_id, "read": True}


# --- 管理端 -------------------------------------------------------------------


def _status_of(starts_at: object, ends_at: object, live: bool) -> str:
    """待生效 / 生效中 / 已结束：此前界面把「未生效」与「已过期」混成一个状态。"""
    if live:
        return "live"
    now = datetime.now(UTC)
    if isinstance(starts_at, datetime) and starts_at > now:
        return "scheduled"
    return "ended"


@router.get("/control/announcements")
def list_announcements_admin(
    actor: AdminReader,
    include_offline: bool = True,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    del actor
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=100)
    live_filter = "" if include_offline else f"AND {_live_clause('a')}"
    try:
        with pg_transaction() as pg:
            rows = pg.execute(
                f"""
                SELECT a.id, a.title, a.body, a.tone, a.starts_at, a.ends_at,
                       a.pinned, creator.username, a.created_at,
                       (SELECT count(DISTINCT r.user_id) FROM announcement_reads r
                         WHERE r.announcement_id = a.id),
                       {_live_clause("a")}
                FROM announcements a
                JOIN users creator ON creator.id = a.created_by_user_id
                WHERE a.deleted_at IS NULL {live_filter}
                ORDER BY a.pinned DESC, a.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (bounded_limit, bounded_offset),
            ).fetchall()
            total_row = pg.execute(
                f"SELECT count(*) FROM announcements a WHERE a.deleted_at IS NULL {live_filter}"
            ).fetchone()
            # 触达率分母：当前启用的客户数（公告面向全部客户）。
            audience_row = pg.execute(
                "SELECT count(*) FROM users WHERE role = 'customer' AND is_active = 1"
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    items = [
        _announcement_row(row) | {"status": _status_of(row[4], row[5], bool(row[10]))}
        for row in rows
    ]
    return {
        "items": items,
        "total": int(total_row[0]) if total_row else 0,
        "audience_total": int(audience_row[0]) if audience_row else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


@router.post("/control/announcements")
def create_announcement(
    body: AnnouncementCreateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        title = body.title.strip()
        if not (1 <= len(title) <= MAX_TITLE_CHARS):
            raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "标题必填且不超过 120 字。")
        text = body.body.strip()
        if not (1 <= len(text) <= MAX_BODY_CHARS):
            raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "正文必填且不超过 4000 字。")
        if body.tone not in TONES:
            raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "色调无效。")
        starts_at = _parse_ts(body.starts_at, field="生效时间") if body.starts_at.strip() else None
        ends_at = _parse_ts(body.ends_at, field="失效时间") if body.ends_at.strip() else None
        if starts_at and ends_at and ends_at <= starts_at:
            raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "失效时间必须晚于生效时间。")
        announcement_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO announcements (id, title, body, tone, starts_at, ends_at, "
            "pinned, created_by_user_id) VALUES (%s,%s,%s,%s,COALESCE(%s,now()),%s,%s,%s)",
            (
                announcement_id,
                title,
                text,
                body.tone,
                starts_at,
                ends_at,
                1 if body.pinned else 0,
                actor.user_id,
            ),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'announcement.create',"
            "'announcement',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                announcement_id,
                json.dumps(
                    {"title": title, "tone": body.tone, "request_id": request_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": announcement_id, "request_id": request_id}

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=201,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )


@router.patch("/control/announcements/{announcement_id}")
def update_announcement(
    announcement_id: str,
    body: AnnouncementUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """部分更新：非空字段覆盖（置顶开关用 pinned 三态区分"不修改"）。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT title, body, tone, starts_at, ends_at, pinned "
            "FROM announcements WHERE id=%s AND deleted_at IS NULL",
            (announcement_id,),
        ).fetchone()
        if row is None:
            raise _http(404, "ANNOUNCEMENT_NOT_FOUND", "公告不存在或已删除。")
        sets: list[str] = []
        params: list[object] = []
        if body.title.strip():
            title = body.title.strip()
            if len(title) > MAX_TITLE_CHARS:
                raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "标题不超过 120 字。")
            sets.append("title=%s")
            params.append(title)
        if body.body.strip():
            text = body.body.strip()
            if len(text) > MAX_BODY_CHARS:
                raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "正文不超过 4000 字。")
            sets.append("body=%s")
            params.append(text)
        if body.tone:
            if body.tone not in TONES:
                raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "色调无效。")
            sets.append("tone=%s")
            params.append(body.tone)
        if body.starts_at.strip():
            sets.append("starts_at=%s")
            params.append(_parse_ts(body.starts_at, field="生效时间"))
        if body.clear_ends_at:
            sets.append("ends_at=NULL")
        elif body.ends_at.strip():
            sets.append("ends_at=%s")
            params.append(_parse_ts(body.ends_at, field="失效时间"))
        if body.pinned is not None:
            sets.append("pinned=%s")
            params.append(1 if body.pinned else 0)
        if not sets:
            raise _http(422, "ANNOUNCEMENT_VALIDATION_FAILED", "没有可更新的字段。")
        params.append(announcement_id)
        conn.execute(
            f"UPDATE announcements SET {', '.join(sets)} WHERE id=%s",  # noqa: S608
            tuple(params),
        )
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'announcement.update',"
            "'announcement',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                announcement_id,
                json.dumps(
                    {"fields": sets, "reason": body.reason.strip(), "request_id": request_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": announcement_id, "request_id": request_id}

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


@router.delete("/control/announcements/{announcement_id}")
def delete_announcement(
    announcement_id: str,
    request: Request,
    response: Response,
    actor: AdminWriter,
    body: AdminWriteRequest,
) -> dict[str, object]:
    """软删（下线）：已读回执保留；客户端立即不可见。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        result = conn.execute(
            "UPDATE announcements SET deleted_at=now() "
            "WHERE id=%s AND deleted_at IS NULL RETURNING id",
            (announcement_id,),
        ).fetchone()
        if result is None:
            raise _http(404, "ANNOUNCEMENT_NOT_FOUND", "公告不存在或已删除。")
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
            "entity_id, metadata_json) VALUES (%s,%s,'announcement.delete',"
            "'announcement',%s,%s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                announcement_id,
                json.dumps(
                    {"reason": body.reason.strip(), "request_id": request_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return {"id": announcement_id, "deleted": True, "request_id": request_id}

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
