"""工作台"提取文案"异步任务路由（script-from-audio）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.auth import ReadDatabase, ShortAuthUser
from app.customer_fence import BusinessDbDep
from app.permissions import require_project_access
from app.script_from_audio import (
    ScriptFromAudioRequest,
    ScriptFromAudioTaskResponse,
    enqueue_script_from_audio_task,
    latest_script_from_audio_task,
    load_script_from_audio_task,
    script_from_audio_task_response,
)

router = APIRouter(prefix="/api")


@router.get("/script-from-audio-tasks/{task_id}/segments")
def read_audio_segments(
    task_id: str,
    conn: ReadDatabase,
    actor: ShortAuthUser,
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
) -> dict[str, object]:
    parent = load_script_from_audio_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(parent["project_id"]),
        action="project.script_from_audio_read",
    )
    if str(parent["created_by_user_id"]) != actor.id:
        raise HTTPException(
            404, detail={"code": "TEXT_TASK_MISSING", "message": "任务不存在或无权访问。"}
        )
    rows = conn.execute(
        "SELECT * FROM asr_segments WHERE task_id=%s AND segment_index>=%s ORDER BY segment_index LIMIT %s",
        (task_id, offset, limit + 1),
    ).fetchall()
    items = []
    for row in rows[:limit]:
        result = json.loads(str(row["result_json"] or "{}"))
        items.append(
            {
                "index": row["segment_index"],
                "start": row["core_start"],
                "end": row["core_end"],
                "status": row["status"],
                "preview": str(result.get("text", ""))[:240],
                "error_code": row["error_code"],
            }
        )
    return {
        "items": items,
        "next_offset": rows[limit]["segment_index"] if len(rows) > limit else None,
    }


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3600000)
    minutes, rest = divmod(rest, 60000)
    whole, fraction = divmod(rest, 1000)
    return f"{hours:02}:{minutes:02}:{whole:02},{fraction:03}"


@router.get("/script-from-audio-tasks/{task_id}/subtitles")
def export_subtitles(task_id: str, conn: ReadDatabase, actor: ShortAuthUser) -> StreamingResponse:
    parent = load_script_from_audio_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(parent["project_id"]),
        action="project.script_from_audio_read",
    )
    if str(parent["created_by_user_id"]) != actor.id:
        raise HTTPException(
            404, detail={"code": "TEXT_TASK_MISSING", "message": "任务不存在或无权访问。"}
        )
    rows = conn.execute(
        "SELECT * FROM asr_segments WHERE task_id=%s ORDER BY segment_index", (task_id,)
    ).fetchall()
    if (
        parent["status"] != "SUCCEEDED"
        or not rows
        or any(not json.loads(str(row["result_json"] or "{}")).get("spans") for row in rows)
    ):
        raise HTTPException(
            409,
            detail={
                "code": "SUBTITLE_TIMESTAMPS_UNAVAILABLE",
                "message": "此文案没有完整可靠的时间戳，可导出 TXT 全文。",
            },
        )

    def chunks() -> Iterator[bytes]:
        cue, text, start, end = 0, "", 0.0, 0.0
        for row in rows:
            offset, core_start, core_end = (
                float(row["input_start"]),
                float(row["core_start"]),
                float(row["core_end"]),
            )
            for span in json.loads(str(row["result_json"]))["spans"]:
                begin, finish = offset + float(span["start"]), offset + float(span["end"])
                if not core_start <= (begin + finish) / 2 < core_end:
                    continue
                if text and (len(text) + len(span["text"]) > 42 or finish - start > 5):
                    cue += 1
                    yield f"{cue}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n\n".encode()
                    text = ""
                if not text:
                    start = max(core_start, begin)
                text += str(span["text"])
                end = min(core_end, finish)
        if text:
            yield f"{cue + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n\n".encode()

    return StreamingResponse(
        chunks(),
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="transcript.srt"'},
    )


@router.post(
    "/projects/{project_id}/script-from-audio",
    response_model=ScriptFromAudioTaskResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def extract_script_from_audio(
    project_id: str,
    request: ScriptFromAudioRequest,
    db: BusinessDbDep,
) -> ScriptFromAudioTaskResponse:
    with db.write() as (conn, actor):
        row = enqueue_script_from_audio_task(
            conn,
            actor=actor,
            project_id=project_id,
            source_asset_id=request.source_asset_id,
            idempotency_key=request.idempotency_key or str(uuid4()),
            processing_version=request.processing_version,
        )
        return script_from_audio_task_response(row, conn=conn)


@router.get(
    "/script-from-audio-tasks/{task_id}",
    response_model=ScriptFromAudioTaskResponse,
)
def read_script_from_audio_task(
    task_id: str,
    conn: ReadDatabase,
    actor: ShortAuthUser,
) -> ScriptFromAudioTaskResponse:
    row = load_script_from_audio_task(conn, task_id)
    require_project_access(
        conn,
        actor=actor,
        project_id=str(row["project_id"]),
        action="project.script_from_audio_read",
    )
    return script_from_audio_task_response(row, conn=conn)


@router.get(
    "/projects/{project_id}/script-from-audio-tasks/latest",
    response_model=ScriptFromAudioTaskResponse | None,
)
def read_latest_script_from_audio_task(
    project_id: str,
    conn: ReadDatabase,
    actor: ShortAuthUser,
) -> ScriptFromAudioTaskResponse | None:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="project.script_from_audio_read",
    )
    row = latest_script_from_audio_task(conn, project_id=project_id)
    return None if row is None else script_from_audio_task_response(row, conn=conn)
