"""长文案按块交付与修订；不可变快照隔离后台任务和用户的后续编辑。"""

from __future__ import annotations

import hashlib
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth import ReadDatabase, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection, BusinessRow
from app.permissions import require_not_auditor, require_project_access

MAX_DOCUMENT_CHARS = 1_000_000
BLOCK_CHARS = 5000
PREVIEW_CHARS = 2000
router = APIRouter(prefix="/api", tags=["text-documents"])


class DocumentRef(BaseModel):
    id: str
    revision: int
    char_count: int
    sha256: str
    immutable: bool


class CreateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(max_length=MAX_DOCUMENT_CHARS)


class EditBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    text: str = Field(max_length=BLOCK_CHARS)


class DocumentRevision(BaseModel):
    revision: int = Field(ge=1)


def document_ref(row: BusinessRow) -> DocumentRef:
    return DocumentRef(**{name: row[name] for name in DocumentRef.model_fields})


def create_document(
    conn: BusinessConnection,
    *,
    project_id: str,
    owner_user_id: str,
    text: str,
    kind: str = "transcript",
    immutable: bool = True,
    document_id: str | None = None,
) -> DocumentRef:
    if len(text) > MAX_DOCUMENT_CHARS:
        raise HTTPException(
            413,
            detail={
                "code": "TEXT_DOCUMENT_TOO_LONG",
                "message": "文案超过一百万字上限，请分开处理。",
            },
        )
    document_id = document_id or str(uuid4())
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = conn.execute("SELECT * FROM text_documents WHERE id=%s", (document_id,)).fetchone()
    if existing is not None:
        if (
            str(existing["sha256"]) != digest
            or str(existing["owner_user_id"]) != owner_user_id
            or str(existing["project_id"]) != project_id
        ):
            raise RuntimeError("文案快照身份冲突。")
        return document_ref(existing)
    row = conn.execute(
        "INSERT INTO text_documents(id,project_id,owner_user_id,kind,immutable,char_count,sha256) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
        (document_id, project_id, owner_user_id, kind, immutable, len(text), digest),
    ).fetchone()
    for index, offset in enumerate(range(0, max(1, len(text)), BLOCK_CHARS)):
        conn.execute(
            "INSERT INTO text_document_blocks(document_id,block_index,text) VALUES (%s,%s,%s)",
            (document_id, index, text[offset : offset + BLOCK_CHARS]),
        )
    return document_ref(row)


def owned_document(
    conn: BusinessConnection, actor: Any, document_id: str, *, write: bool = False
) -> BusinessRow:
    row = conn.execute(
        "SELECT * FROM text_documents WHERE id=%s" + (" FOR UPDATE" if write else ""),
        (document_id,),
    ).fetchone()
    if row is None or str(row["owner_user_id"]) != actor.id:
        raise HTTPException(
            404, detail={"code": "TEXT_DOCUMENT_MISSING", "message": "文案不存在或无权访问。"}
        )
    require_project_access(
        conn, actor=actor, project_id=str(row["project_id"]), action="project.text_document"
    )
    if write:
        require_not_auditor(
            conn,
            actor=actor,
            action="project.text_document_edit",
            entity_type="project",
            entity_id=str(row["project_id"]),
        )
    return cast(BusinessRow, row)


def document_text(conn: BusinessConnection, document_id: str) -> str:
    return "".join(
        str(row["text"])
        for row in conn.execute(
            "SELECT text FROM text_document_blocks WHERE document_id=%s ORDER BY block_index",
            (document_id,),
        ).fetchall()
    )


def snapshot_document(
    conn: BusinessConnection, actor: Any, document_id: str, revision: int
) -> DocumentRef:
    row = owned_document(conn, actor, document_id, write=True)
    if int(row["revision"]) != revision:
        raise HTTPException(
            409,
            detail={
                "code": "TEXT_DOCUMENT_REVISION_CONFLICT",
                "message": "文案已更新，请刷新后重试。",
            },
        )
    if row["immutable"]:
        return document_ref(row)
    return create_document(
        conn,
        project_id=str(row["project_id"]),
        owner_user_id=actor.id,
        text=document_text(conn, document_id),
        kind="snapshot",
        document_id=str(uuid5(NAMESPACE_URL, f"text-snapshot:{document_id}:{revision}")),
    )


@router.post("/projects/{project_id}/text-documents", response_model=DocumentRef)
def create_text_document(
    project_id: str, request: CreateDocument, db: BusinessDbDep
) -> DocumentRef:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="project.text_document_edit",
            entity_type="project",
            entity_id=project_id,
        )
        require_project_access(
            conn, actor=actor, project_id=project_id, action="project.text_document"
        )
        return create_document(
            conn,
            project_id=project_id,
            owner_user_id=actor.id,
            text=request.text,
            kind="draft",
            immutable=False,
        )


@router.get("/text-documents/{document_id}", response_model=DocumentRef)
def get_document(document_id: str, conn: ReadDatabase, actor: ShortAuthUser) -> DocumentRef:
    return document_ref(owned_document(conn, actor, document_id))


@router.get("/text-documents/{document_id}/blocks")
def get_blocks(
    document_id: str,
    conn: ReadDatabase,
    actor: ShortAuthUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(1, ge=1, le=20),
    search: str = Query("", max_length=200),
) -> dict[str, object]:
    row = owned_document(conn, actor, document_id)
    blocks = conn.execute(
        "SELECT block_index,text FROM text_document_blocks WHERE "
        "document_id=%s AND block_index>=%s "
        "AND (%s='' OR strpos(text,%s)>0) ORDER BY block_index LIMIT %s",
        (document_id, offset, search, search, limit + 1),
    ).fetchall()
    return {
        "document": document_ref(row),
        "blocks": [dict(b) for b in blocks[:limit]],
        "next_offset": int(blocks[limit]["block_index"]) if len(blocks) > limit else None,
    }


@router.post("/text-documents/{document_id}/fork", response_model=DocumentRef)
def fork_document(document_id: str, request: DocumentRevision, db: BusinessDbDep) -> DocumentRef:
    with db.write() as (conn, actor):
        row = owned_document(conn, actor, document_id, write=True)
        if int(row["revision"]) != request.revision:
            raise HTTPException(
                409,
                detail={
                    "code": "TEXT_DOCUMENT_REVISION_CONFLICT",
                    "message": "文案已更新，请刷新。",
                },
            )
        return create_document(
            conn,
            project_id=str(row["project_id"]),
            owner_user_id=actor.id,
            text=document_text(conn, document_id),
            kind="draft",
            immutable=False,
        )


@router.patch("/text-documents/{document_id}/blocks/{block_index}", response_model=DocumentRef)
def edit_block(
    document_id: str, block_index: int, request: EditBlock, db: BusinessDbDep
) -> DocumentRef:
    with db.write() as (conn, actor):
        row = owned_document(conn, actor, document_id, write=True)
        if row["immutable"] or int(row["revision"]) != request.revision:
            raise HTTPException(
                409,
                detail={
                    "code": "TEXT_DOCUMENT_REVISION_CONFLICT",
                    "message": "文案快照不可修改，或版本已变化。请创建副本后编辑。",
                },
            )
        current = conn.execute(
            "SELECT text FROM text_document_blocks WHERE document_id=%s AND block_index=%s",
            (document_id, block_index),
        ).fetchone()
        if current is None:
            raise HTTPException(
                404, detail={"code": "TEXT_BLOCK_MISSING", "message": "文案段落不存在。"}
            )
        count = int(row["char_count"]) - len(current["text"]) + len(request.text)
        if count > MAX_DOCUMENT_CHARS:
            raise HTTPException(
                413, detail={"code": "TEXT_DOCUMENT_TOO_LONG", "message": "文案超过一百万字上限。"}
            )
        conn.execute(
            "UPDATE text_document_blocks SET text=%s WHERE document_id=%s AND block_index=%s",
            (request.text, document_id, block_index),
        )
        digest = hashlib.sha256(document_text(conn, document_id).encode("utf-8")).hexdigest()
        result = conn.execute(
            "UPDATE text_documents SET "
            "revision=revision+1,char_count=%s,sha256=%s,updated_at=now() "
            "WHERE id=%s RETURNING *",
            (count, digest, document_id),
        ).fetchone()
        return document_ref(result)


@router.get("/text-documents/{document_id}/export")
def export_document(
    document_id: str, conn: ReadDatabase, actor: ShortAuthUser
) -> StreamingResponse:
    owned_document(conn, actor, document_id)
    # 在连接释放前读取有硬上限的文案块，流式响应不持有业务事务。
    blocks = conn.execute(
        "SELECT text FROM text_document_blocks WHERE document_id=%s ORDER BY block_index",
        (document_id,),
    ).fetchall()
    return StreamingResponse(
        (str(row["text"]).encode("utf-8") for row in blocks),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="transcript.txt"'},
    )


@router.post("/text-documents/{document_id}/snapshot", response_model=DocumentRef)
def freeze_document(document_id: str, request: DocumentRevision, db: BusinessDbDep) -> DocumentRef:
    with db.write() as (conn, actor):
        return snapshot_document(conn, actor, document_id, request.revision)


@router.post("/text-tasks/{kind}/{task_id}/cancel")
def cancel_text_task(
    kind: Literal["asr", "rewrite"], task_id: str, db: BusinessDbDep
) -> dict[str, bool]:
    table = "script_from_audio_tasks" if kind == "asr" else "script_rewrite_tasks"
    with db.write() as (conn, actor):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=%s FOR UPDATE", (task_id,)).fetchone()  # noqa: S608
        if row is None or str(row["created_by_user_id"]) != actor.id:
            raise HTTPException(
                404, detail={"code": "TEXT_TASK_MISSING", "message": "任务不存在或无权访问。"}
            )
        project_id = str(row["project_id"])
        require_project_access(
            conn, actor=actor, project_id=project_id, action="project.text_cancel"
        )
        require_not_auditor(
            conn,
            actor=actor,
            action="project.text_cancel",
            entity_type="project",
            entity_id=project_id,
        )
        if row["processing_version"] != "segmented-v1":
            raise HTTPException(
                409,
                detail={
                    "code": "TEXT_TASK_CANCEL_UNAVAILABLE",
                    "message": "此任务不支持分段取消，请等待处理完成。",
                },
            )
        if row["status"] in {"PENDING", "RUNNING"}:
            conn.execute(
                f"UPDATE {table} SET cancel_requested_at=COALESCE(cancel_requested_at,now()),updated_at=now() WHERE id=%s",
                (task_id,),
            )  # noqa: S608
        return {"accepted": True}


class RetryTextTask(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.post("/text-tasks/{kind}/{task_id}/retry")
def retry_text_task(
    kind: Literal["asr", "rewrite"], task_id: str, request: RetryTextTask, db: BusinessDbDep
) -> dict[str, object]:
    from app.usage_billing import accept_operation

    table = "script_from_audio_tasks" if kind == "asr" else "script_rewrite_tasks"
    segments = "asr_segments" if kind == "asr" else "rewrite_segments"
    with db.write() as (conn, actor):
        row = conn.execute(f"SELECT * FROM {table} WHERE id=%s", (task_id,)).fetchone()  # noqa: S608
        if row is None or str(row["created_by_user_id"]) != actor.id:
            raise HTTPException(
                404, detail={"code": "TEXT_TASK_MISSING", "message": "任务不存在或无权访问。"}
            )
        project_id = str(row["project_id"])
        require_not_auditor(
            conn,
            actor=actor,
            action="project.text_retry",
            entity_type="project",
            entity_id=project_id,
        )
        require_project_access(
            conn, actor=actor, project_id=project_id, action="project.text_retry"
        )
        conn.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (project_id,))
        row = conn.execute(f"SELECT * FROM {table} WHERE id=%s FOR UPDATE", (task_id,)).fetchone()  # noqa: S608
        replay = conn.execute(
            "SELECT 1 FROM text_task_retries WHERE kind=%s AND task_id=%s AND idempotency_key=%s",
            (kind, task_id, request.idempotency_key),
        ).fetchone()
        if replay or row["status"] in {"PENDING", "RUNNING"}:
            return {"task_id": task_id, "accepted": True}
        if row["processing_version"] != "segmented-v1" or row["status"] != "FAILED":
            raise HTTPException(
                409,
                detail={
                    "code": "TEXT_TASK_RETRY_UNAVAILABLE",
                    "message": "当前任务不支持分段重试。",
                },
            )
        active = conn.execute(
            f"SELECT 1 FROM {table} WHERE project_id=%s AND status IN ('PENDING','RUNNING') LIMIT 1",
            (project_id,),
        ).fetchone()  # noqa: S608
        if active:
            raise HTTPException(
                409,
                detail={"code": "TEXT_TASK_ALREADY_RUNNING", "message": "当前项目已有任务进行中。"},
            )
        inflight = conn.execute(
            f"SELECT 1 FROM {segments} WHERE task_id=%s AND status IN ('SUBMITTING','UNCERTAIN'"
            + (",'POLLING')" if kind == "asr" else ")")
            + " LIMIT 1",
            (task_id,),
        ).fetchone()  # noqa: S608
        if inflight:
            raise HTTPException(
                409,
                detail={
                    "code": "TEXT_TASK_AWAITING_SETTLEMENT",
                    "message": "请等待已提交分段核对完成后再重试。",
                },
            )
        service = "asr" if kind == "asr" else "rewrite"
        billing_round = int(
            conn.execute(
                "SELECT COALESCE(max(billing_round),0)+1 FROM billing_operations WHERE service=%s AND source_id=%s",
                (service, task_id),
            ).fetchone()[0]
        )
        units = (
            conn.execute(
                "SELECT max(core_end) FROM asr_segments WHERE task_id=%s", (task_id,)
            ).fetchone()[0]
            if kind == "asr"
            else 1
        )
        if units is None:
            raise HTTPException(409, detail={"code": "TEXT_TASK_SOURCE_UNAVAILABLE", "message": "来源处理记录不完整，请重新上传来源。"})
        accept_operation(
            conn,
            user_id=actor.id,
            service=service,
            source_id=task_id,
            units=units,
            billing_round=billing_round,
        )
        if kind == "asr":
            # 新轮次使用新对象键；旧轮次的清理与迟到 Worker 都不能触碰新输入。
            conn.execute(
                "UPDATE asr_segments SET status='PREPARING',provider_task_id=NULL,billing_attempt_id=NULL,submitted_at=NULL,error_code=NULL,"
                "object_key='projects/' || %s || '/asr/' || task_id || '/round-' || %s || '/segment-' || segment_index::text || '.m4a' "
                "WHERE task_id=%s AND status IN ('FAILED','CANCELLED')",
                (project_id, str(billing_round), task_id),
            )
        else:
            conn.execute(
                "UPDATE rewrite_segments SET status='READY',billing_attempt_id=NULL,submitted_at=NULL,error_code=NULL WHERE task_id=%s AND status IN ('FAILED','CANCELLED')",
                (task_id,),
            )
        conn.execute(
            f"UPDATE {table} SET status='PENDING',cancel_requested_at=NULL,processing_phase='QUEUED',provider_started_at=NULL,"
            "locked_by=NULL,locked_until=NULL,next_attempt_at=NULL,completed_at=NULL,reopen_count=0,error_code=NULL,error_message_redacted=NULL,updated_at=now() WHERE id=%s",
            (task_id,),
        )  # noqa: S608
        conn.execute(
            "INSERT INTO text_task_retries(kind,task_id,idempotency_key,owner_user_id,billing_round) VALUES (%s,%s,%s,%s,%s)",
            (kind, task_id, request.idempotency_key, actor.id, billing_round),
        )
        return {"task_id": task_id, "accepted": True}
