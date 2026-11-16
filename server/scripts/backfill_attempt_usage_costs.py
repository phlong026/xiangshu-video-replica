"""批量确认 UNKNOWN 调用的实际用量并按目录单价确认成本（审计修复第三批）。

与 ``backfill_operation_costs``（M2，确认"用量已知、单价曾缺失"的调用）互补：
本工具面向"任务已成功交付但用量没回传"的 UNKNOWN 调用——从**源任务表的
已交付事实数据**找回实际用量（视频=成片秒数、口播/转写=时长、按次科目=1 次），
再按目录单价确认成本。两类工具覆盖后，成本核对队列不再有"单价缺失/用量
缺失"型积压。

红线：
- 只处理**底层操作已终态**（非 PENDING）的调用，绝不触碰在途预留；
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
_SECOND_USAGE_SOURCES: dict[str, tuple[str, str]] = {
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
        "JOIN oral_tasks ora ON ora.id = o.source_id",
        "COALESCE(ora.duration_sec, 0)",
        "AND ora.status = 'SUCCEEDED' AND ora.duration_sec IS NOT NULL",
    ),
    "asr": (
        "JOIN script_from_audio_tasks asr ON asr.id = o.source_id",
        "COALESCE((asr.result_json::jsonb->>'duration_sec')::numeric, 0)",
        "AND asr.status = 'SUCCEEDED' AND asr.result_json::jsonb->>'duration_sec' IS NOT NULL",
    ),
}


def _pending_stats(conn: BusinessConnection, service: str) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE ba.usage IS NOT NULL) AS with_usage,
          count(*) FILTER (WHERE ba.usage IS NULL) AS without_usage
        FROM billing_attempts ba
        JOIN billing_operations o ON o.id = ba.operation_id
        LEFT JOIN billing_evidence e ON e.attempt_id = ba.id
        WHERE ba.service = %s
          AND ba.state = 'UNKNOWN'
          AND ba.cost_fen IS NULL
          AND e.attempt_id IS NULL
          AND o.state <> 'PENDING'
        """,
        (service,),
    ).fetchone()
    return {
        "with_usage": int(row["with_usage"]),
        "without_usage": int(row["without_usage"]),
    }


def _fetch_batch(
    conn: BusinessConnection,
    *,
    service: str,
    usage_expr_sql: str,
    task_join_sql: str,
    extra_where_sql: str,
    limit: int,
) -> list[psycopg.Row]:
    return conn.execute(
        f"""
        SELECT ba.id AS attempt_id, ba.operation_id,
               {usage_expr_sql} AS usage
        FROM billing_attempts ba
        JOIN billing_operations o ON o.id = ba.operation_id
        LEFT JOIN billing_evidence e ON e.attempt_id = ba.id
        {task_join_sql}
        WHERE ba.service = %s
          AND ba.state = 'UNKNOWN'
          AND ba.cost_fen IS NULL
          AND e.attempt_id IS NULL
          AND o.state <> 'PENDING'
          {extra_where_sql}
        ORDER BY ba.completed_at, ba.id
        LIMIT %s
        FOR UPDATE OF ba SKIP LOCKED
        """,  # noqa: S608 - 白名单表/列名 + 固定片段
        (service, limit),
    ).fetchall()


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
    """单科目回填；返回 (确认条数, 成本合计分)。"""
    is_call_unit = service in _CALL_UNIT_SERVICES
    usage_expr = "1" if is_call_unit else _SECOND_USAGE_SOURCES[service][1]
    task_join = "" if is_call_unit else _SECOND_USAGE_SOURCES[service][0]
    extra_where = "" if is_call_unit else _SECOND_USAGE_SOURCES[service][2]

    total = 0
    cost_total = Decimal(0)
    while total < limit:
        # 每批一个独立事务：批内锁定行 → 写证据+审计 → 提交；重跑幂等。
        with conn.transaction():
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
    services = [s.strip() for s in args.services.split(",") if s.strip()]
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

    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            actor = conn.execute(
                "SELECT id FROM users WHERE id = %s", (args.actor_user_id,)
            ).fetchone()
            if actor is None:
                print(
                    f"error: --actor-user-id {args.actor_user_id!r} is not a known user",
                    file=sys.stderr,
                )
                return 1
            tariffs: dict[str, Decimal] = {}
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

        if not args.apply:
            print("DRY-RUN（未写库）。加 --apply 执行。services=" + ",".join(services))
            return 0

        actor_id = str(actor["id"])
        for service in services:
            unit_cost = tariffs[service]
            tariff = read_tariff(conn, service)
            reference = (
                args.reference + f":{service}@v{tariff.version}"
                if tariff is not None
                else args.reference + f":{service}"
            )
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
