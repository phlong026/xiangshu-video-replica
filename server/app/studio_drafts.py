"""C7 studio drafts domain: cloud persistence for the V1.4 copy workshop.

The copy workshop keeps its working draft (whole ``StudioDraft`` snapshot as
opaque JSON) and an explicitly saved script list. Drafts are strictly
user-scoped — no project access checks, because drafts may exist before any
project does (viral-video / persona entry points). The generation gate is
untouched: only the explicit "确认终稿" publish path writes project script
versions via ``/projects/{id}/scripts``; this module never does.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor

DraftKind = Literal["copy", "oral", "replica", "publishing"]
DRAFT_KINDS: tuple[str, ...] = ("copy", "oral", "replica", "publishing")

MAX_DRAFT_PAYLOAD_BYTES = 512_000
MAX_SAVED_SCRIPT_TEXT_CHARS = 100_000
MAX_SAVED_SCRIPTS_PER_USER = 50

# 墓碑拒绝窗（T35）：物理删除改成墓碑后，删除后一小段时间内的无版本盲写
# （旧客户端的 last-write-wins PUT）一律 409，挡住「在途旧 PUT 复活已放弃草稿」；
# 窗口过后放行盲写，让未升级的旧客户端仍能开始真正的新稿。窗口只影响盲写；
# 带 expected_revision 的写入永远按 revision 判定，与本窗口无关。
_TOMBSTONE_WINDOW_ENV = "VIDEO_REPLICA_DRAFT_TOMBSTONE_WINDOW_SECONDS"
_DEFAULT_TOMBSTONE_WINDOW_SECONDS = 600.0


def _tombstone_window_seconds() -> float:
    raw = os.environ.get(_TOMBSTONE_WINDOW_ENV, "").strip()
    if not raw:
        return _DEFAULT_TOMBSTONE_WINDOW_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TOMBSTONE_WINDOW_SECONDS
    return value if value >= 0 else _DEFAULT_TOMBSTONE_WINDOW_SECONDS


_UTC_NOW_LOCK = threading.Lock()
_UTC_NOW_LAST = ""


def _utc_now_text() -> str:
    """Microsecond-precision timestamp; CURRENT_TIMESTAMP alone is
    second-granular and cannot break ties for uuid-keyed rows.

    同一进程内保证严格递增：Windows 系统时钟粒度约 15.6ms，同一 tick 内的写入会
    拿到完全相同的 updated_at，而并列组内的兜底排序键是随机 UUID，「最新写入排
    最前」就变成掷硬币（CW-058 flaky 的根因）。同 tick 内按微秒顺延消除并列；
    跨进程仍依赖系统时钟（Linux 微秒级，实际不会并列）。
    """
    global _UTC_NOW_LAST
    with _UTC_NOW_LOCK:
        value = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        if value <= _UTC_NOW_LAST:
            previous = datetime.strptime(_UTC_NOW_LAST, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
            value = (previous + timedelta(microseconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
        _UTC_NOW_LAST = value
        return value


class StudioDraftUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, object]
    script_confirmed: bool = False
    expected_revision: int | None = Field(default=None, ge=0)


class StudioDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_kind: str
    payload: dict[str, object]
    script_confirmed: bool
    revision: int
    updated_at: str


class SavedScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=MAX_SAVED_SCRIPT_TEXT_CHARS)
    original: str | None = None
    version: int = Field(default=1, ge=1)
    ip_id: str | None = None
    source_project_id: str | None = None
    source_kind: Literal["viral", "project", "link", "upload"] | None = None


class SavedScriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str
    title: str
    text: str
    original: str | None
    version: int
    ip_id: str | None
    source_project_id: str | None
    source_kind: str | None
    created_at: str
    updated_at: str


class SavedScriptListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SavedScriptResponse]


def _draft_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "STUDIO_DRAFT_NOT_FOUND", "message": "暂无云端草稿。"},
    )


def _draft_tombstoned(revision: int) -> HTTPException:
    """放弃围栏（T35）：墓碑行拒绝旧写回流，附带当前版本号供新稿续写。"""
    return HTTPException(
        409,
        detail={
            "code": "STUDIO_DRAFT_TOMBSTONED",
            "message": "云端草稿刚被放弃，本次内容不再写回；重新开始的内容可直接保存。",
            "current_revision": revision,
        },
    )


def _draft_conflict(row: sqlite3.Row | None) -> HTTPException:
    revision = 0 if row is None else int(row["revision"])
    return HTTPException(
        409,
        detail={
            "code": "STUDIO_DRAFT_CONFLICT",
            "message": "云端草稿已在其他窗口更新，请重新加载后再保存。",
            "current_revision": revision,
        },
    )


def _tombstone_recent(deleted_at: str | None) -> bool:
    if not deleted_at:
        return False
    try:
        deleted = datetime.strptime(deleted_at, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        # 时间戳解析不了的存量行按「刚放弃」处理，宁严勿漏。
        return True
    return (datetime.now(UTC) - deleted).total_seconds() < _tombstone_window_seconds()


def _validate_draft_kind(kind: str) -> None:
    if kind not in DRAFT_KINDS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "STUDIO_DRAFT_KIND_INVALID",
                "message": "不支持的工作区草稿类型。",
            },
        )


def load_studio_draft(
    conn: BusinessConnection,
    *,
    actor_id: str,
    kind: str,
) -> StudioDraftResponse:
    row = conn.execute(
        """
        SELECT * FROM studio_drafts
        WHERE user_id = %s AND draft_kind = %s AND deleted_at IS NULL
        """,
        (actor_id, kind),
    ).fetchone()
    if row is None:
        raise _draft_not_found()
    return _draft_response(row)


def save_studio_draft(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    kind: str,
    request: StudioDraftUpsertRequest,
) -> StudioDraftResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.draft_write",
        entity_type="studio_draft",
        entity_id=kind,
    )
    _validate_draft_kind(kind)
    payload_text = json.dumps(request.payload, ensure_ascii=False, sort_keys=True)
    if len(payload_text.encode("utf-8")) > MAX_DRAFT_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "STUDIO_DRAFT_PAYLOAD_TOO_LARGE",
                "message": "草稿内容过大，请精简后再保存。",
            },
        )
    confirmed_flag = 1 if request.script_confirmed else 0
    if kind == "publishing" and request.expected_revision is None:
        raise HTTPException(
            422,
            detail={
                "code": "STUDIO_DRAFT_REVISION_REQUIRED",
                "message": "请先加载发布草稿后再保存。",
            },
        )
    if request.expected_revision is not None:
        if request.expected_revision == 0:
            row = conn.execute(
                """INSERT INTO studio_drafts
                (id, user_id, draft_kind, payload, script_confirmed, revision)
                VALUES (%s, %s, %s, %s, %s, 1)
                ON CONFLICT (user_id, draft_kind) DO NOTHING RETURNING *""",
                (str(uuid4()), actor.id, kind, payload_text, confirmed_flag),
            ).fetchone()
        else:
            # 命中 revision 即写入：活行是普通保存，墓碑行是「知道放弃后版本号的
            # 真正新稿」复活（T35）。旧版本号的迟到写在版本推进后必然错位 409。
            row = conn.execute(
                """UPDATE studio_drafts SET payload = %s, script_confirmed = %s,
                revision = revision + 1, deleted_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND draft_kind = %s AND revision = %s RETURNING *""",
                (payload_text, confirmed_flag, actor.id, kind, request.expected_revision),
            ).fetchone()
        if row is None:
            current = conn.execute(
                """SELECT revision, deleted_at FROM studio_drafts
                WHERE user_id = %s AND draft_kind = %s""",
                (actor.id, kind),
            ).fetchone()
            if current is not None and current["deleted_at"] is not None:
                raise _draft_tombstoned(int(current["revision"]))
            raise _draft_conflict(current)
        conn.commit()
        return _draft_response(row)
    existing = conn.execute(
        """SELECT revision, deleted_at FROM studio_drafts
        WHERE user_id = %s AND draft_kind = %s""",
        (actor.id, kind),
    ).fetchone()
    if existing is not None and existing["deleted_at"] is not None:
        # 盲写撞上新鲜墓碑：这极可能是放弃前发出的在途旧 PUT（T35 的
        # PUT→DELETE 竞态），拒绝写回；窗口过后视为旧客户端开真正的新稿。
        if _tombstone_recent(str(existing["deleted_at"])):
            raise _draft_tombstoned(int(existing["revision"]))
    conn.execute(
        """
        INSERT INTO studio_drafts (
            id, user_id, draft_kind, payload, script_confirmed, revision
        ) VALUES (%s, %s, %s, %s, %s, 1)
        ON CONFLICT (user_id, draft_kind) DO UPDATE SET
            payload = excluded.payload,
            script_confirmed = excluded.script_confirmed,
            revision = studio_drafts.revision + 1,
            deleted_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(uuid4()), actor.id, kind, payload_text, confirmed_flag),
    )
    row = conn.execute(
        "SELECT * FROM studio_drafts WHERE user_id = %s AND draft_kind = %s",
        (actor.id, kind),
    ).fetchone()
    if row is None:  # pragma: no cover - upsert always yields the row
        raise _draft_not_found()
    conn.commit()
    return _draft_response(row)


def delete_studio_draft(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    kind: str,
) -> int:
    """放弃云端草稿：物理行保留为墓碑（清空内容、版本推进），返回新版本号。

    墓碑让删除之后迟到的旧写无所遁形：带旧 revision 的条件写必然错位 409，
    盲写在拒绝窗内同样 409（T35）。重复放弃视为成功，返回当前墓碑版本——
    客户端「已不存在也算放弃成功」的既有语义不变。
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.draft_delete",
        entity_type="studio_draft",
        entity_id=kind,
    )
    _validate_draft_kind(kind)
    row = conn.execute(
        """
        UPDATE studio_drafts SET deleted_at = %s, payload = %s,
            revision = revision + 1, updated_at = CURRENT_TIMESTAMP
        WHERE user_id = %s AND draft_kind = %s AND deleted_at IS NULL
        RETURNING revision
        """,
        (_utc_now_text(), "{}", actor.id, kind),
    ).fetchone()
    if row is not None:
        conn.commit()
        return int(row["revision"])
    tombstone = conn.execute(
        """SELECT revision FROM studio_drafts
        WHERE user_id = %s AND draft_kind = %s AND deleted_at IS NOT NULL""",
        (actor.id, kind),
    ).fetchone()
    if tombstone is not None:
        return int(tombstone["revision"])
    raise _draft_not_found()


def list_saved_scripts(
    conn: BusinessConnection,
    *,
    actor_id: str,
) -> SavedScriptListResponse:
    rows = conn.execute(
        """
        SELECT * FROM studio_saved_scripts
        WHERE user_id = %s
        ORDER BY updated_at DESC, id DESC
        LIMIT %s
        """,
        (actor_id, MAX_SAVED_SCRIPTS_PER_USER),
    ).fetchall()
    return SavedScriptListResponse(items=[_saved_script_response(row) for row in rows])


def load_saved_script(
    conn: BusinessConnection,
    *,
    actor_id: str,
    script_id: str,
) -> SavedScriptResponse:
    row = conn.execute(
        "SELECT * FROM studio_saved_scripts WHERE user_id = %s AND script_id = %s",
        (actor_id, script_id),
    ).fetchone()
    if row is None:
        raise HTTPException(
            404,
            detail={
                "code": "STUDIO_SAVED_SCRIPT_NOT_FOUND",
                "message": "该保存文案不存在或已被删除。",
            },
        )
    return _saved_script_response(row)


def save_saved_script(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    request: SavedScriptRequest,
) -> SavedScriptResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.saved_script_write",
        entity_type="studio_saved_script",
        entity_id=request.script_id,
    )
    if not request.text.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "STUDIO_SAVED_SCRIPT_TEXT_REQUIRED",
                "message": "文案内容不能为空。",
            },
        )
    now = _utc_now_text()
    conn.execute(
        """
        INSERT INTO studio_saved_scripts (
            id, user_id, script_id, title, text, original, version,
            ip_id, source_project_id, source_kind, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, script_id) DO UPDATE SET
            title = excluded.title,
            text = excluded.text,
            original = excluded.original,
            version = excluded.version,
            ip_id = excluded.ip_id,
            source_project_id = excluded.source_project_id,
            source_kind = excluded.source_kind,
            updated_at = excluded.updated_at
        """,
        (
            str(uuid4()),
            actor.id,
            request.script_id,
            request.title,
            request.text,
            request.original,
            request.version,
            request.ip_id,
            request.source_project_id,
            request.source_kind,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM studio_saved_scripts
        WHERE user_id = %s AND script_id = %s
        """,
        (actor.id, request.script_id),
    ).fetchone()
    if row is None:  # pragma: no cover - upsert always yields the row
        raise _draft_not_found()
    conn.commit()
    return _saved_script_response(row)


def delete_saved_script(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    script_id: str,
) -> None:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.saved_script_delete",
        entity_type="studio_saved_script",
        entity_id=script_id,
    )
    cursor = conn.execute(
        "DELETE FROM studio_saved_scripts WHERE user_id = %s AND script_id = %s",
        (actor.id, script_id),
    )
    if getattr(cursor, "rowcount", 0) == 0:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "STUDIO_SAVED_SCRIPT_NOT_FOUND",
                "message": "该保存文案不存在或已被删除。",
            },
        )
    conn.commit()


def _draft_response(row: sqlite3.Row) -> StudioDraftResponse:
    return StudioDraftResponse(
        draft_kind=str(row["draft_kind"]),
        payload=cast(dict[str, object], json.loads(str(row["payload"]))),
        script_confirmed=bool(int(row["script_confirmed"])),
        revision=int(row["revision"]),
        updated_at=str(row["updated_at"]),
    )


def _saved_script_response(row: sqlite3.Row) -> SavedScriptResponse:
    return SavedScriptResponse(
        script_id=str(row["script_id"]),
        title=str(row["title"]),
        text=str(row["text"]),
        original=None if row["original"] is None else str(row["original"]),
        version=int(row["version"]),
        ip_id=None if row["ip_id"] is None else str(row["ip_id"]),
        source_project_id=(
            None if row["source_project_id"] is None else str(row["source_project_id"])
        ),
        source_kind=None if row["source_kind"] is None else str(row["source_kind"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
