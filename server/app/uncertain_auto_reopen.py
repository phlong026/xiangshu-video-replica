"""SUBMISSION_UNCERTAIN 自动处置（2026-11-11 极简化 WP-C）：参数读取 + 自动重开扫掠.

共享文案缓存退役后，「提交结果不可证明」不再有人工核对队列——超阈值自动
重开新一轮计费并重跑；重开次数封顶，耗尽落 FAILED（观测页可见）。资金安全
三条纪律（Codex S 级评审阻塞 1/2/4 的闭环）：

1. 重开前先把该任务**全部旧轮** PENDING 计费单按失败收口（只退不收）、旧供应商
   调用按 UNKNOWN 语义收口——否则新轮成功时遗留旧 PENDING 单会按同一任务的
   SUCCEEDED 再结算一次（双扣）。
2. 每个候选用独立 SAVEPOINT：预留/CAS/复位/审计原子完成，单个候选失败
   （如低余额 402）只跳过自己并延期（locked_until 顶到阈值处），不回滚整轮
   对账、不打断 worker。
3. ``reopen_count < 上限`` 守卫在 CAS 条件里防并发重复重开；上限耗尽落
   FAILED 终态并写审计，观测页可见。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import uuid4

import psycopg

from app.db_portable import BusinessConnection

DEFAULT_AUTO_REOPEN_MINUTES = 30
DEFAULT_AUTO_REOPEN_MAX = 2


class UncertainAutoReopenParams(TypedDict):
    threshold_minutes: int
    max_reopens: int


def read_uncertain_auto_reopen_params(
    conn: BusinessConnection | psycopg.Connection,
) -> UncertainAutoReopenParams:
    """runtime_settings 单行表可能缺行（全新库）：缺行按默认值，与迁移一致.

    两种连接都会传入（worker 用 BusinessConnection，控制面用裸 psycopg），所以
    按位置取列——BusinessRow 与 tuple 行都支持整数下标。
    """
    row = conn.execute(
        "SELECT uncertain_auto_reopen_minutes, uncertain_auto_reopen_max "
        "FROM runtime_settings WHERE id=1"
    ).fetchone()
    if row is None:
        return {
            "threshold_minutes": DEFAULT_AUTO_REOPEN_MINUTES,
            "max_reopens": DEFAULT_AUTO_REOPEN_MAX,
        }
    return {
        "threshold_minutes": int(row[0]),
        "max_reopens": int(row[1]),
    }


def _audit(
    conn: BusinessConnection,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, object],
) -> None:
    """后台扫掠审计：无登录主体（actor NULL），不提交（复用外层对账事务）."""
    conn.execute(
        "INSERT INTO audit_logs (id, actor_user_id, action, entity_type, "
        "entity_id, metadata_json) VALUES (%s,NULL,%s,%s,%s,%s)",
        (
            str(uuid4()),
            action,
            entity_type,
            entity_id,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        ),
    )


def _time_after_minutes(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def _next_round(conn: BusinessConnection, *, service: str, source_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(max(billing_round),0) FROM billing_operations "
        "WHERE service=%s AND source_id=%s",
        (service, source_id),
    ).fetchone()
    return int(row[0]) + 1


def _close_stale_rounds(
    conn: BusinessConnection, *, service: str, source_id: str, user_id: str
) -> None:
    """旧轮收口（Codex 阻塞 1）：全部 PENDING 计费单按失败释放（只退不收），
    旧供应商调用按 UNKNOWN 语义收口（成本记为未知，不虚构也不清零）。"""
    conn.execute(
        "UPDATE billing_attempts a SET usage=NULL,cost_fen=NULL,state='UNKNOWN',"
        "completed_at=now() FROM billing_operations o "
        "WHERE a.operation_id=o.id AND o.service=%s AND o.source_id=%s "
        "AND o.user_id=%s AND a.state='PENDING'",
        (service, source_id, user_id),
    )
    stale = conn.execute(
        "SELECT id FROM billing_operations WHERE service=%s AND source_id=%s "
        "AND user_id=%s AND state='PENDING' FOR UPDATE",
        (service, source_id, user_id),
    ).fetchall()
    if not stale:
        return
    from app.usage_billing import finish_operation

    for stale_row in stale:
        finish_operation(conn, operation_id=str(stale_row["id"]), units=0, succeeded=False)


def _reopen_one(
    conn: BusinessConnection,
    *,
    task_id: str,
    table: str,
    audit_entity: str,
    service: str,
    units: int,
    reset_sql: str,
    reset_params: tuple[object, ...],
    max_reopens: int,
    threshold_minutes: int,
) -> str:
    """单个候选的原子重开；返回动作（'reopened'|'deferred'）.

    SAVEPOINT 隔离：402（余额不足）等受理失败只回滚本候选并延期（locked_until
    顶到阈值处，状态保持 SUBMISSION_UNCERTAIN），下轮扫掠到延期点后再试，
    不回滚整轮对账、不打断 worker（Codex 阻塞 2）。
    """
    conn.execute("SAVEPOINT reopen_candidate")
    try:
        owner = conn.execute(
            f"SELECT created_by_user_id FROM {table} WHERE id=%s FOR UPDATE",  # noqa: S608
            (task_id,),
        ).fetchone()
        if owner is None:
            conn.execute("RELEASE SAVEPOINT reopen_candidate")
            return "deferred"
        user_id = str(owner["created_by_user_id"])
        _close_stale_rounds(conn, service=service, source_id=task_id, user_id=user_id)
        from app.usage_billing import accept_operation

        accept_operation(
            conn,
            user_id=user_id,
            service=service,
            source_id=task_id,
            units=units,
            billing_round=_next_round(conn, service=service, source_id=task_id),
        )
        conn.execute(reset_sql, reset_params)
        _audit(
            conn,
            action=f"{service}.auto_reopened",
            entity_type=audit_entity,
            entity_id=task_id,
            metadata={
                "threshold_minutes": threshold_minutes,
                "max_reopens": max_reopens,
                "reason": "SUBMISSION_UNCERTAIN 超阈值自动重开（无人值守处置）",
            },
        )
        conn.execute("RELEASE SAVEPOINT reopen_candidate")
        return "reopened"
    except Exception as exc:
        conn.execute("ROLLBACK TO SAVEPOINT reopen_candidate")
        deferred_until = _time_after_minutes(threshold_minutes)
        conn.execute(
            f"UPDATE {table} SET locked_until=%s,updated_at=now() "  # noqa: S608
            "WHERE id=%s AND status='SUBMISSION_UNCERTAIN'",
            (deferred_until, task_id),
        )
        try:
            _audit(
                conn,
                action=f"{service}.auto_reopen_deferred",
                entity_type=audit_entity,
                entity_id=task_id,
                metadata={
                    "reason": f"{type(exc).__name__}: 重开受理失败（低余额或账号状态变化），已延期",
                    "retry_after_minutes": threshold_minutes,
                },
            )
        except Exception:
            pass
        return "deferred"


_ASR_RESET_SQL = (
    "UPDATE script_from_audio_tasks SET status='PENDING',"
    "provider_started_at=NULL,provider_task_id=NULL,completed_at=NULL,"
    "error_code=NULL,error_message_redacted=NULL,retryable=0,"
    "locked_by=NULL,locked_until=NULL,next_attempt_at=NULL,"
    "reopen_count=reopen_count+1,updated_at=now() "
    "WHERE id=%s AND status='SUBMISSION_UNCERTAIN' AND reopen_count<%s"
)

_REWRITE_RESET_SQL = (
    "UPDATE script_rewrite_tasks SET status='PENDING',"
    "provider_started_at=NULL,provider_task_id=NULL,completed_at=NULL,"
    "result_json=NULL,error_code=NULL,error_message_redacted=NULL,"
    "retryable=0,locked_by=NULL,locked_until=NULL,"
    "reopen_count=reopen_count+1,updated_at=now() "
    "WHERE id=%s AND status='SUBMISSION_UNCERTAIN' AND reopen_count<%s"
)


def reopen_uncertain_script_from_audio_tasks(
    conn: BusinessConnection,
    *,
    threshold_minutes: int,
    max_reopens: int,
    limit: int = 50,
) -> dict[str, int]:
    """超阈值 UNCERTAIN 转写任务自动重开（units 按资产时长，同首次受理口径）.

    阈值=0 表示关闭自动重开（调用方直接短路）。时间比较显式 ::timestamptz
    （completed_at 为 TEXT 列）；候选排除同项目有活跃兄弟的行，防撞
    「一个项目至多一条在制任务」唯一索引。
    """
    outcome = {"reopened": 0, "deferred": 0, "exhausted": 0}
    if threshold_minutes <= 0 or max_reopens <= 0:
        return outcome
    candidates = conn.execute(
        "SELECT id FROM script_from_audio_tasks t "
        "WHERE t.status='SUBMISSION_UNCERTAIN' "
        "AND t.completed_at::timestamptz < now() - (%s * interval '1 minute') "
        "AND (t.locked_until IS NULL OR t.locked_until::timestamptz < now()) "
        "AND t.reopen_count < %s "
        "AND NOT EXISTS (SELECT 1 FROM script_from_audio_tasks s "
        "    WHERE s.project_id=t.project_id AND s.status IN ('PENDING','RUNNING')) "
        "ORDER BY t.completed_at LIMIT %s FOR UPDATE OF t SKIP LOCKED",
        (threshold_minutes, max_reopens, limit),
    ).fetchall()
    for candidate in candidates:
        task_id = str(candidate["id"])
        units = _asset_duration_units(conn, task_id=task_id)
        outcome[
            _reopen_one(
                conn,
                task_id=task_id,
                table="script_from_audio_tasks",
                audit_entity="script_from_audio_task",
                service="asr",
                units=units,
                reset_sql=_ASR_RESET_SQL,
                reset_params=(task_id, max_reopens),
                max_reopens=max_reopens,
                threshold_minutes=threshold_minutes,
            )
        ] += 1
    exhausted = conn.execute(
        "UPDATE script_from_audio_tasks SET status='FAILED',"
        "error_code='SCRIPT_FROM_AUDIO_AUTO_REOPEN_EXHAUSTED',"
        "error_message_redacted='自动重试次数已达上限，请联系管理员核实。',"
        "retryable=0,updated_at=now() "
        "WHERE status='SUBMISSION_UNCERTAIN' "
        "AND completed_at::timestamptz < now() - (%s * interval '1 minute') "
        "AND reopen_count>=%s "
        "AND NOT EXISTS (SELECT 1 FROM script_from_audio_tasks s "
        "    WHERE s.project_id=script_from_audio_tasks.project_id "
        "    AND s.status IN ('PENDING','RUNNING'))",
        (threshold_minutes, max_reopens),
    )
    if exhausted.rowcount:
        outcome["exhausted"] += exhausted.rowcount
        _audit(
            conn,
            action="script_from_audio.auto_reopen_exhausted",
            entity_type="script_from_audio_task",
            entity_id="batch",
            metadata={"count": exhausted.rowcount, "max_reopens": max_reopens},
        )
    return outcome


def _asset_duration_units(conn: BusinessConnection, *, task_id: str) -> int:
    """重开按资产时长重新预留（与首次受理同一口径；0 会被受理拒绝则跳过）."""
    row = conn.execute(
        "SELECT a.metadata_json FROM script_from_audio_tasks t "
        "JOIN assets a ON a.id=t.source_asset_id AND a.project_id=t.project_id "
        "WHERE t.id=%s",
        (task_id,),
    ).fetchone()
    if row is None:
        return 0
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except (ValueError, TypeError):
        return 0
    return int(metadata.get("duration_seconds") or metadata.get("duration_sec") or 0)


def reopen_uncertain_script_rewrite_tasks(
    conn: BusinessConnection,
    *,
    threshold_minutes: int,
    max_reopens: int,
    limit: int = 50,
) -> dict[str, int]:
    """超阈值 UNCERTAIN 改写任务自动重开（每轮按次计费，units=1）."""
    outcome = {"reopened": 0, "deferred": 0, "exhausted": 0}
    if threshold_minutes <= 0 or max_reopens <= 0:
        return outcome
    candidates = conn.execute(
        "SELECT id FROM script_rewrite_tasks t "
        "WHERE t.status='SUBMISSION_UNCERTAIN' "
        "AND t.completed_at::timestamptz < now() - (%s * interval '1 minute') "
        "AND (t.locked_until IS NULL OR t.locked_until::timestamptz < now()) "
        "AND t.reopen_count < %s "
        "AND NOT EXISTS (SELECT 1 FROM script_rewrite_tasks s "
        "    WHERE s.project_id=t.project_id AND s.status IN ('PENDING','RUNNING')) "
        "ORDER BY t.completed_at LIMIT %s FOR UPDATE OF t SKIP LOCKED",
        (threshold_minutes, max_reopens, limit),
    ).fetchall()
    for candidate in candidates:
        task_id = str(candidate["id"])
        outcome[
            _reopen_one(
                conn,
                task_id=task_id,
                table="script_rewrite_tasks",
                audit_entity="script_rewrite_task",
                service="rewrite",
                units=1,
                reset_sql=_REWRITE_RESET_SQL,
                reset_params=(task_id, max_reopens),
                max_reopens=max_reopens,
                threshold_minutes=threshold_minutes,
            )
        ] += 1
    exhausted = conn.execute(
        "UPDATE script_rewrite_tasks SET status='FAILED',"
        "error_code='SCRIPT_REWRITE_AUTO_REOPEN_EXHAUSTED',"
        "error_message_redacted='自动重试次数已达上限，请联系管理员核实。',"
        "retryable=0,updated_at=now() "
        "WHERE status='SUBMISSION_UNCERTAIN' "
        "AND completed_at::timestamptz < now() - (%s * interval '1 minute') "
        "AND reopen_count>=%s "
        "AND NOT EXISTS (SELECT 1 FROM script_rewrite_tasks s "
        "    WHERE s.project_id=script_rewrite_tasks.project_id "
        "    AND s.status IN ('PENDING','RUNNING'))",
        (threshold_minutes, max_reopens),
    )
    if exhausted.rowcount:
        outcome["exhausted"] += exhausted.rowcount
        _audit(
            conn,
            action="script_rewrite.auto_reopen_exhausted",
            entity_type="script_rewrite_task",
            entity_id="batch",
            metadata={"count": exhausted.rowcount, "max_reopens": max_reopens},
        )
    return outcome
