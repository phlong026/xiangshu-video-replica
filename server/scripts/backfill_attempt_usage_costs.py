"""批量确认 UNKNOWN 调用的实际用量并按目录单价确认成本（审计修复第三批）。

与 ``backfill_operation_costs``（M2，确认"用量已知、单价曾缺失"的调用）互补：
本工具面向"任务已成功交付但用量没回传"的 UNKNOWN 调用——从**源任务表的
已交付事实数据**找回实际用量（视频=成片秒数、口播/转写=时长、按次科目=1 次），
再按目录单价确认成本。两类工具覆盖后，成本核对队列不再有"单价缺失/用量
缺失"型积压。

红线：
- 只处理**业务已成功交付**（SUCCEEDED）的调用，绝不触碰在途或失败请求；
- 用量必须能从源任务表的事实数据取到（如成片秒数），取不到就跳过报告，
  绝不编造用量；
- 单价取目录当前值（unit_cost_fen 已配置才跑），成本 = 用量 × 单价，与
  complete_attempt 运行时口径一致；
- 逐行写 billing_evidence + 审计，批次独立提交，重跑幂等（已有证据的调用
  被候选查询排除）。

用法（默认 dry-run，--apply 才写库）::

    python -m scripts.backfill_attempt_usage_costs \
        --services video_768p,analysis,character \
        --actor-user-id <uuid> --reason "按源任务交付用量批量确认成本"
    python -m scripts.backfill_attempt_usage_costs ... --apply --limit 500

只输出聚合计数，不打印任何客户身份或金额明细。
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from uuid import uuid4

import psycopg

from app.billing_catalog import read_tariff
from app.db_pg import CliDatabaseConfigError, resolve_cli_pg_dsn
from app.db_portable import BusinessConnection

_BATCH_SIZE = 300

# 按次/张计的科目：attempt 处于 UNKNOWN 即供应商调用已发生，用量记 1 次。
# second 类科目不在此列（从源任务表取实际时长）。
_CALL_UNIT_SERVICES = frozenset(
    {
        "analysis",
        "character",
        "first_frame",
        "rewrite",
        "prompt_optimize",
        "link_resolution",
        "avatar_clone",
        "voice_clone",
    }
)

# second 类科目：源任务表 JOIN 片段与用量表达式（要求已成功交付且时长非空）。
_SECOND_USAGE_SOURCES: dict[str, tuple[str, str, str]] = {
    "video_768p": (
        "JOIN generation_tasks gen ON gen.id = o.source_id",
        "COALESCE(gen.actual_output_seconds, 0)",
        "AND gen.status = 'SUCCEEDED' AND gen.actual_output_seconds IS NOT NULL",
    ),
    "video_2k": (
        "JOIN generation_tasks gen ON gen.id = o.source_id",
        "COALESCE(gen.actual_output_seconds, 0)",
        "AND gen.status = 'SUCCEEDED' AND gen.actual_output_seconds IS NOT NULL",
    ),
    "video_768p_long": (
        "JOIN generation_tasks gen ON gen.id = o.source_id",
        "COALESCE(gen.actual_output_seconds, 0)",
        "AND gen.status = 'SUCCEEDED' AND gen.actual_output_seconds IS NOT NULL",
    ),
    "oral": (
        "JOIN oral_tasks ora ON ora.id = o.source_id "
        "JOIN assets oral_asset ON oral_asset.id = ora.result_asset_id",
        "COALESCE(ora.duration_sec, 0)",
        "AND ora.status = 'SUCCEEDED' AND ora.duration_sec IS NOT NULL "
        "AND ora.result_asset_id IS NOT NULL "
        "AND NULLIF(BTRIM(oral_asset.storage_uri), '') IS NOT NULL",
    ),
    "asr": (
        "JOIN script_from_audio_tasks asr ON asr.id = o.source_id",
        "COALESCE((asr.result_json::jsonb->>'duration_sec')::numeric, 0)",
        "AND asr.status = 'SUCCEEDED' AND asr.result_json::jsonb->>'duration_sec' IS NOT NULL",
    ),
}


def _usage_rule(service: str) -> tuple[str, str, str, bool]:
    """返回 (usage 表达式、任务 JOIN、额外条件、是否按次科目)。"""
    if service in _CALL_UNIT_SERVICES:
        return "COALESCE(ba.usage, 1)", "", "", True
    task_join, usage_expr, extra_where = _SECOND_USAGE_SOURCES[service]
    return usage_expr, task_join, extra_where, False


def _pending_stats(
    conn: BusinessConnection,
    *,
    service: str,
    task_join_sql: str,
    extra_where_sql: str,
    is_call_unit: bool,
) -> dict[str, int]:
    """报告已成功交付 UNKNOWN 操作中可确认与缺少源任务用量的数量。

    一项操作的同一科目可能有多次 UNKNOWN attempt；只要其中任一 attempt
    已有实际成本或 evidence，交付事实都不足以判断另一条不确定重试是否已到达
    供应商，故整项操作跳过，留待人工核对。
    """
    base_sql = """
        FROM billing_attempts ba
        JOIN billing_operations o ON o.id = ba.operation_id
        WHERE ba.service = %s
          AND ba.state = 'UNKNOWN'
          AND ba.cost_fen IS NULL
          AND o.state = 'SUCCEEDED'
          AND NOT EXISTS (
              SELECT 1
              FROM billing_evidence existing_evidence
              JOIN billing_attempts charged_attempt
                ON charged_attempt.id = existing_evidence.attempt_id
              WHERE charged_attempt.operation_id = o.id
                AND charged_attempt.service = ba.service
          )
          AND NOT EXISTS (
              SELECT 1
              FROM billing_attempts known_attempt
              WHERE known_attempt.operation_id = o.id
                AND known_attempt.service = ba.service
                AND known_attempt.cost_fen IS NOT NULL
          )
    """
    total_row = conn.execute(
        "SELECT COUNT(DISTINCT o.id) AS operations " + base_sql,
        (service,),
    ).fetchone()
    total = int(total_row["operations"])
    if is_call_unit:
        return {"with_usage": total, "without_usage": 0}

    with_usage_row = conn.execute(
        f"""
        SELECT COUNT(DISTINCT o.id) AS operations
        FROM billing_attempts ba
        JOIN billing_operations o ON o.id = ba.operation_id
        {task_join_sql}
        WHERE ba.service = %s
          AND ba.state = 'UNKNOWN'
          AND ba.cost_fen IS NULL
          AND o.state = 'SUCCEEDED'
          AND NOT EXISTS (
              SELECT 1
              FROM billing_evidence existing_evidence
              JOIN billing_attempts charged_attempt
                ON charged_attempt.id = existing_evidence.attempt_id
              WHERE charged_attempt.operation_id = o.id
                AND charged_attempt.service = ba.service
          )
          AND NOT EXISTS (
              SELECT 1
              FROM billing_attempts known_attempt
              WHERE known_attempt.operation_id = o.id
                AND known_attempt.service = ba.service
                AND known_attempt.cost_fen IS NOT NULL
          )
          {extra_where_sql}
        """,  # noqa: S608 - JOIN/条件仅取自本模块白名单
        (service,),
    ).fetchone()
    with_usage = int(with_usage_row["operations"])
    return {"with_usage": with_usage, "without_usage": total - with_usage}


def _fetch_batch(
    conn: BusinessConnection,
    *,
    service: str,
    usage_expr_sql: str,
    task_join_sql: str,
    extra_where_sql: str,
    limit: int,
) -> list[psycopg.Row]:
    """每个操作只取一条 UNKNOWN 调用（DISTINCT ON）。

    同一操作多轮重试调用会产生多条 UNKNOWN attempt——若逐条按全量时长确认
    会重复计成本。正确语义：**每个操作确认一次**，成本 = 任务交付量 × 单价，
    即该任务的真实平台总成本；其余 attempt 放弃确认，留痕在队列外。
    """
    return conn.execute(
        f"""
        WITH selected_operations AS (
            SELECT o.id
            FROM billing_operations o
            WHERE o.state = 'SUCCEEDED'
              AND NOT EXISTS (
                  SELECT 1
                  FROM billing_evidence existing_evidence
                  JOIN billing_attempts charged_attempt
                    ON charged_attempt.id = existing_evidence.attempt_id
                  WHERE charged_attempt.operation_id = o.id
                    AND charged_attempt.service = %s
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM billing_attempts known_attempt
                  WHERE known_attempt.operation_id = o.id
                    AND known_attempt.service = %s
                    AND known_attempt.cost_fen IS NOT NULL
              )
              AND EXISTS (
                  SELECT 1
                  FROM billing_attempts ba
                  {task_join_sql}
                  WHERE ba.operation_id = o.id
                    AND ba.service = %s
                    AND ba.state = 'UNKNOWN'
                    AND ba.cost_fen IS NULL
                    {extra_where_sql}
              )
            ORDER BY o.id
            LIMIT %s
            FOR NO KEY UPDATE SKIP LOCKED
        )
        SELECT DISTINCT ON (ba.operation_id)
               ba.id AS attempt_id, ba.operation_id,
               {usage_expr_sql} AS usage
        FROM selected_operations selected
        JOIN billing_operations o ON o.id = selected.id
        JOIN billing_attempts ba ON ba.operation_id = o.id
        {task_join_sql}
        WHERE ba.service = %s
          AND ba.state = 'UNKNOWN'
          AND ba.cost_fen IS NULL
          {extra_where_sql}
        ORDER BY ba.operation_id, ba.completed_at DESC, ba.id DESC
        """,  # noqa: S608 - JOIN/条件仅取自本模块白名单
        (service, service, service, limit, service),
    ).fetchall()


def _operation_service_has_confirmed_cost(
    conn: BusinessConnection, *, operation_id: str, service: str
) -> bool:
    """返回某操作的同科目是否已有实际成本或人工/批处理证据。"""
    return (
        conn.execute(
            "SELECT 1 FROM billing_attempts attempt "
            "LEFT JOIN billing_evidence evidence ON evidence.attempt_id = attempt.id "
            "WHERE attempt.operation_id = %s AND attempt.service = %s "
            "AND (attempt.cost_fen IS NOT NULL OR evidence.id IS NOT NULL) LIMIT 1",
            (operation_id, service),
        ).fetchone()
        is not None
    )


def _backfill_service(
    conn: BusinessConnection,
    *,
    service: str,
    unit_cost: Decimal,
    actor_id: str,
    reason: str,
    reference: str,
    limit: int,
) -> tuple[int, Decimal]:
    """单科目回填；返回 (确认操作数, 成本合计分)。"""
    usage_expr, task_join, extra_where, _ = _usage_rule(service)
    total = 0
    cost_total = Decimal(0)
    while total < limit:
        # autocommit 连接上每个显式 raw transaction 都是独立事务：
        # 批内锁定行 → 写证据+审计 → 提交；外呼无、无长事务。
        with conn.raw.transaction():
            rows = _fetch_batch(
                conn,
                service=service,
                usage_expr_sql=usage_expr,
                task_join_sql=task_join,
                extra_where_sql=extra_where,
                limit=min(_BATCH_SIZE, limit - total),
            )
            if not rows:
                return total, cost_total
            for row in rows:
                operation_id = str(row["operation_id"])
                # 与人工补录端点共用锁；拿锁后重查，避免人工先确认某次重试、
                # 批处理又把同一交付结果写到另一条 retry attempt。
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    ("billing:evidence:" + operation_id,),
                )
                if _operation_service_has_confirmed_cost(
                    conn, operation_id=operation_id, service=service
                ):
                    continue
                cost = Decimal(str(row["usage"])) * unit_cost
                evidence_id = str(uuid4())
                inserted = conn.execute(
                    "INSERT INTO billing_evidence(id,operation_id,attempt_id,"
                    "units,cost_fen,reference,reason,actor_user_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (attempt_id) DO NOTHING RETURNING id",
                    (
                        evidence_id,
                        str(row["operation_id"]),
                        str(row["attempt_id"]),
                        None,
                        cost,
                        reference,
                        reason,
                        actor_id,
                    ),
                ).fetchone()
                if inserted is None:
                    continue
                conn.execute(
                    "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,"
                    "entity_id,metadata_json) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        str(uuid4()),
                        actor_id,
                        "billing.evidence.append",
                        "billing_operation",
                        str(row["operation_id"]),
                        json.dumps(
                            {
                                "evidence_id": evidence_id,
                                "reference": reference,
                                "reason": reason,
                                "batch": "backfill_attempt_usage_costs",
                            }
                        ),
                    ),
                )
                total += 1
                cost_total += cost
    return total, cost_total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Confirm UNKNOWN attempt usage from source tasks at catalog price."
    )
    parser.add_argument(
        "--services",
        required=True,
        help="comma-separated service list, e.g. video_768p,analysis,character",
    )
    parser.add_argument("--actor-user-id", required=True, help="users.id of the admin running this")
    parser.add_argument("--reason", required=True, help="non-blank evidence reason")
    parser.add_argument(
        "--reference",
        default="attempt-usage-backfill",
        help="evidence reference prefix",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument("--limit", type=int, default=2000, help="per-service cap")
    parser.add_argument("--apply", action="store_true", help="write evidence rows; default dry-run")
    args = parser.parse_args(argv)

    if not args.reason.strip():
        print("error: --reason must be non-blank", file=sys.stderr)
        return 1
    if args.limit < 1:
        print("error: --limit must be positive", file=sys.stderr)
        return 1
    # 去重：重复科目会让 limit 语义失真
    services = list(dict.fromkeys(s.strip() for s in args.services.split(",") if s.strip()))
    if not services:
        print("error: --services is empty", file=sys.stderr)
        return 1
    unknown = [
        s for s in services if s not in _CALL_UNIT_SERVICES and s not in _SECOND_USAGE_SOURCES
    ]
    if unknown:
        print(
            "error: no usage-derivation rule for: " + ", ".join(unknown),
            file=sys.stderr,
        )
        return 1

    try:
        database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # autocommit=True：每个 raw transaction 都是独立显式事务，
    # 没有外层隐式事务包裹，批次真正逐批提交。
    with psycopg.connect(database_url, autocommit=True) as raw:
        conn = BusinessConnection.postgres(raw)
        with raw.transaction():
            actor = conn.execute(
                "SELECT id FROM users WHERE id = %s AND role = 'admin'",
                (args.actor_user_id,),
            ).fetchone()
            if actor is None:
                print(
                    "error: --actor-user-id must identify an administrator",
                    file=sys.stderr,
                )
                return 1
            # 一次冻结全部科目的单价与版本（单位：分）
            tariffs: dict[str, Decimal] = {}
            versions: dict[str, int] = {}
            for service in services:
                tariff = read_tariff(conn, service)
                if tariff is None or tariff.unit_cost_fen is None:
                    print(
                        f"error: service {service!r} has no configured unit_cost_fen; "
                        "configure it first via PUT /api/control/billing/tariff",
                        file=sys.stderr,
                    )
                    return 1
                tariffs[service] = Decimal(str(tariff.unit_cost_fen))
                versions[service] = int(tariff.version)

        if not args.apply:
            # dry-run 预演：打印每个科目的可确认操作数与无法定价的用量缺失数
            for service in services:
                _, task_join, extra_where, is_call_unit = _usage_rule(service)
                stats = _pending_stats(
                    conn,
                    service=service,
                    task_join_sql=task_join,
                    extra_where_sql=extra_where,
                    is_call_unit=is_call_unit,
                )
                print(
                    f"service={service} unit_cost_fen={tariffs[service]} "
                    f"operations_to_confirm={stats['with_usage']} "
                    f"usage_missing={stats['without_usage']}"
                )
            print("DRY-RUN（未写库）。加 --apply 执行。")
            return 0

        actor_id = str(actor["id"])
        for service in services:
            unit_cost = tariffs[service]
            version = versions[service]
            reference = f"{args.reference}:{service}@v{version}"
            confirmed, cost_total = _backfill_service(
                conn,
                service=service,
                unit_cost=unit_cost,
                actor_id=actor_id,
                reason=args.reason,
                reference=reference,
                limit=args.limit,
            )
            print(f"service={service} confirmed={confirmed} total_cost_fen={cost_total}")

    print("ALL SERVICES DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
