"""M4 队列与 Worker 观测（ADM-11，只读聚合，零迁移）。

管理端此前对任务积压"不可见"——三个 worker（generation/oral/publish）的
队列深度、租约年龄、隔离区只能靠客户报障发现。本模块把四类事实聚合为
一个观测端点：

- **队列深度**：视频任务、口播任务、采集/转存刷新任务、发布记录按状态计数；
- **高龄任务**：排队/运行超过阈值（视频 30 分钟、口播 15 分钟、发布 10 分钟）
  的最早任务年龄，越老越说明 worker 掉线或卡死；
- **oral 隔离区**：SUBMISSION_UNCERTAIN 的口播任务与 SUBMISSION_UNKNOWN 的
  分身/声音——它们冻结了客户预扣积分，是必须人工 reconcile 的队列；
- **素材准备租约**：viral_media_preparations 中租约过期仍 CLAIMED 的行数。

全部为 COUNT/MAX 聚合读，不引入新表；数据不可用时 503 fail-closed。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.admin_auth_routes import AdminReader
from app.admin_customer_metrics import utc_text_timestamp
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-worker-observations"])

# 高龄阈值（分钟）：超过即视为需要人工关注，不是精确 SLA。
_VIDEO_STALE_MINUTES = 30
_ORAL_STALE_MINUTES = 15
_PUBLISH_STALE_MINUTES = 10


def _minutes(alias: str, column: str = "created_at", *, text_utc: bool = True) -> str:
    # 旧表时间列是 TEXT 的 CURRENT_TIMESTAMP（UTC 无后缀）——先归一成
    # timestamptz 再参与 now() 运算；新表（publish_records）本就是 timestamptz。
    ts = utc_text_timestamp(f"{alias}.{column}") if text_utc else f"{alias}.{column}"
    return f"EXTRACT(EPOCH FROM (now()-({ts})))/60"


@router.get("/worker-observations")
def read_worker_observations(actor: AdminReader) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            video = conn.execute(
                f"""
                SELECT status, count(*), COALESCE(MAX({_minutes("t")}), 0)
                FROM generation_tasks t GROUP BY status ORDER BY status
                """
            ).fetchall()
            oral = conn.execute(
                f"""
                SELECT status, count(*), COALESCE(MAX({_minutes("t")}), 0)
                FROM oral_tasks t GROUP BY status ORDER BY status
                """
            ).fetchall()
            refresh = conn.execute(
                """
                SELECT status, sort, count(*) FROM viral_refresh_tasks
                GROUP BY status, sort ORDER BY status, sort
                """
            ).fetchall()
            publish = conn.execute(
                """
                SELECT status, count(*),
                       COALESCE(MAX(EXTRACT(EPOCH FROM (now()-t.created_at))/60), 0)
                FROM publish_records t GROUP BY status ORDER BY status
                """
            ).fetchall()
            quarantine_oral = conn.execute(
                """
                SELECT count(*) FROM oral_tasks
                WHERE status = 'SUBMISSION_UNCERTAIN'
                   OR submission_state = 'SUBMISSION_UNKNOWN'
                """
            ).fetchone() or (0,)
            quarantine_clones = conn.execute(
                """
                SELECT
                  (SELECT count(*) FROM oral_avatars
                    WHERE submission_state = 'SUBMISSION_UNKNOWN'),
                  (SELECT count(*) FROM oral_voices
                    WHERE submission_state = 'SUBMISSION_UNKNOWN')
                """
            ).fetchone() or (0, 0)
            # 准备表没有租约时间列（租约在应用层轮换）：长期 RUNNING 即视为
            # 卡住的准备（正常秒级完成），10 分钟阈值远大于正常耗时的上界。
            stale_leases = conn.execute(
                f"""
                SELECT count(*), COALESCE(MAX({_minutes("p")}), 0)
                FROM viral_media_preparations p
                WHERE p.status = 'RUNNING'
                  AND ({utc_text_timestamp("p.created_at")}) < now()-interval '10 minutes'
                """
            ).fetchone() or (0, 0)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503, "WORKER_OBSERVATIONS_UNAVAILABLE", "队列观测需要 PostgreSQL 运行时。"
        ) from exc

    def _by_status(rows: list[tuple[object, ...]]) -> list[dict[str, object]]:
        return [
            {
                "status": str(r[0]),
                "count": int(r[1]),  # type: ignore[call-overload]
                "oldest_minutes": round(float(r[2]), 1),  # type: ignore[arg-type]
            }
            for r in rows
        ]

    def _oldest(rows: list[dict[str, object]], statuses: tuple[str, ...]) -> float | None:
        ages = [
            float(r["oldest_minutes"])  # type: ignore[arg-type]
            for r in rows
            if r["status"] in statuses
        ]
        return max(ages) if ages else None

    video_rows = _by_status(video)
    oral_rows = _by_status(oral)
    publish_rows = _by_status(publish)
    return {
        "queues": {
            "video": video_rows,
            "oral": oral_rows,
            "viralRefresh": [
                {"status": str(r[0]), "sort": str(r[1]), "count": int(r[2])} for r in refresh
            ],
            "publish": publish_rows,
        },
        "attention": {
            # 高龄运行/排队任务：worker 掉线或卡死的最直接信号。
            "videoStaleMinutes": _oldest(video_rows, ("QUEUED", "SUBMITTING", "RUNNING")),
            "videoStaleThresholdMinutes": _VIDEO_STALE_MINUTES,
            "oralStaleMinutes": _oldest(
                oral_rows, ("QUEUED", "SUBMITTING", "RUNNING", "ARCHIVING")
            ),
            "oralStaleThresholdMinutes": _ORAL_STALE_MINUTES,
            "publishStaleMinutes": _oldest(publish_rows, ("queued", "publishing")),
            "publishStaleThresholdMinutes": _PUBLISH_STALE_MINUTES,
            "oralQuarantinedTasks": int(quarantine_oral[0]),
            "oralQuarantinedAvatars": int(quarantine_clones[0]),
            "oralQuarantinedVoices": int(quarantine_clones[1]),
            "stuckPreparations": int(stale_leases[0]),
            "stuckPreparationOldestMinutes": round(float(stale_leases[1]), 1),
        },
        "rule": (
            "队列计数按任务状态实时聚合；高龄=排队/运行状态中最早任务的分钟数，"
            "超过阈值建议先查 worker 进程与日志。隔离区任务冻结了客户预扣积分，"
            "需按失败诊断页的处理建议人工核对。长期 RUNNING 的素材准备视为卡住"
            "（正常秒级完成），可等待冷却后被重新认领或人工重试。"
        ),
    }
