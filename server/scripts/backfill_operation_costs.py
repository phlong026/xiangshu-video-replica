"""批量补录历史 UNKNOWN 调用成本（审计修复 2026-11-15，模块 M2）。

背景：quality_inspection / analysis_repair / analysis_repair_deepseek 三个
内部科目的计费目录长期未配单价，begin_attempt 快照到的 unit_cost_fen 为空，
complete_attempt 把这些调用记成 state=UNKNOWN、cost_fen=NULL——每个月堆积
数百条"成本待核对"，人工在成本核对页逐条补录根本追不上，月度毛利率因此
长期"待核对"。

本脚本在管理员把目录单价配好之后运行（PUT /api/control/billing/tariff），
按目录里"现在"的单价为历史 UNKNOWN 调用补记 billing_evidence（cost =
usage × unit_cost_fen，与 complete_attempt 的运行时口径一致）。写入走与
人工单条补录完全相同的表和审计动作（billing.evidence.append），已确认成本
的调用天然幂等跳过；usage 为空的调用无法推算，只报告不代答。

用法（默认 dry-run，只打印规模；加 --apply 才写库）::

    # 先看规模
    python -m scripts.backfill_operation_costs --service quality_inspection \
        --actor-user-id <uuid> --reason "质检单价已配置为 5 分/次，批量补录"
    # 确认后落库
    python -m scripts.backfill_operation_costs --service quality_inspection \
        --actor-user-id <uuid> --reason "…" --apply --limit 2000

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

# 事务批量：每批一个提交，批内写一条审计；中断时已提交批次各自有审计可查。
_BATCH_SIZE = 500


def _candidates(conn: BusinessConnection, *, service: str, limit: int) -> dict[str, int]:
    """统计口径与成本核对页一致：成本未知的已完成调用。

    各桶条件写在 FILTER 里而不是外层 WHERE——外层过滤会让 pending 桶恒为 0，
    运营看不到"还有多少在途请求将来才需要补"。
    """
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE o.state <> 'PENDING' AND a.cost_fen IS NULL
                             AND e.attempt_id IS NULL AND a.usage IS NOT NULL) AS backfillable,
          count(*) FILTER (WHERE o.state <> 'PENDING' AND a.cost_fen IS NULL
                             AND e.attempt_id IS NULL AND a.usage IS NULL) AS unmetered,
          count(*) FILTER (WHERE o.state = 'PENDING') AS pending,
          count(*) FILTER (WHERE o.state <> 'PENDING') AS total_finished
        FROM billing_attempts a
        JOIN billing_operations o ON o.id = a.operation_id
        LEFT JOIN billing_evidence e ON e.attempt_id = a.id
        WHERE a.service = %s
        """,
        (service,),
    ).fetchone()
    return {
        "backfillable": int(row["backfillable"]),
        "unmetered": int(row["unmetered"]),
        "pending": int(row["pending"]),
        "total_finished": int(row["total_finished"]),
    }


def _fetch_batch(conn: BusinessConnection, *, service: str, limit: int) -> list[psycopg.Row]:
    return conn.execute(
        """
        SELECT a.id AS attempt_id, a.operation_id, a.usage
        FROM billing_attempts a
        JOIN billing_operations o ON o.id = a.operation_id
        LEFT JOIN billing_evidence e ON e.attempt_id = a.id
        WHERE a.service = %s
          AND a.state = 'UNKNOWN'
          AND a.cost_fen IS NULL
          AND e.attempt_id IS NULL
          AND a.usage IS NOT NULL
          AND o.state <> 'PENDING'
        ORDER BY a.completed_at, a.id
        LIMIT %s
        FOR UPDATE OF a SKIP LOCKED
        """,  # noqa: S608 - 固定列清单
        (service, limit),
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill UNKNOWN attempt costs from the configured tariff."
    )
    parser.add_argument("--service", required=True, help="e.g. quality_inspection")
    parser.add_argument(
        "--actor-user-id",
        required=True,
        help="users.id of the admin running this backfill (evidence FK)",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="non-blank reason recorded on every evidence row",
    )
    parser.add_argument(
        "--reference",
        default="",
        help="evidence reference; defaults to tariff-backfill:<service>@v<version>",
    )
    parser.add_argument(
        "--database-url",
        default="",
        help="PostgreSQL DSN (defaults to VIDEO_REPLICA_DATABASE_URL)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=2000,
        help="maximum attempts backfilled in this run",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the evidence rows; without this flag the run is read-only",
    )
    args = parser.parse_args(argv)

    if not args.reason.strip():
        print("error: --reason must be non-blank", file=sys.stderr)
        return 1
    if args.limit < 1:
        print("error: --limit must be positive", file=sys.stderr)
        return 1

    try:
        database_url = resolve_cli_pg_dsn(args.database_url)
    except CliDatabaseConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with psycopg.connect(database_url) as raw:
        conn = BusinessConnection.postgres(raw)
        # 检查阶段一个短事务：actor/tariff/规模，只读。
        with raw.transaction():
            actor = conn.execute(
                "SELECT id FROM users WHERE id = %s", (args.actor_user_id,)
            ).fetchone()
            if actor is None:
                print(
                    f"error: --actor-user-id {args.actor_user_id!r} is not a known user",
                    file=sys.stderr,
                )
                return 1
            tariff = read_tariff(conn, args.service)
            if tariff is None or tariff.unit_cost_fen is None:
                print(
                    f"error: service {args.service!r} has no configured unit_cost_fen; "
                    "configure it first via PUT /api/control/billing/tariff",
                    file=sys.stderr,
                )
                return 1
            unit_cost = Decimal(str(tariff.unit_cost_fen))
            stats = _candidates(conn, service=args.service, limit=args.limit)
            print(
                f"service={args.service} tariff_v{tariff.version} "
                f"unit_cost_fen={unit_cost} "
                f"backfillable={stats['backfillable']} unmetered={stats['unmetered']} "
                f"pending={stats['pending']} total_finished={stats['total_finished']}"
            )
            if not args.apply:
                # 只读模式（默认）：打印规模即退出。
                return 0
        if stats["backfillable"] == 0:
            print("nothing to backfill")
            return 0

        reference = args.reference or (f"tariff-backfill:{args.service}@v{tariff.version}")
        backfilled = 0
        total_cost_fen = Decimal(0)
        while backfilled < args.limit:
            # 每批独立提交：批内锁定行（FOR UPDATE SKIP LOCKED）+ 写证据 + 写审计；
            # 中断时已提交批次各自带审计，重跑幂等（已确认的调用会被查询排除）。
            with raw.transaction():
                rows = _fetch_batch(
                    conn,
                    service=args.service,
                    limit=min(_BATCH_SIZE, args.limit - backfilled),
                )
                if not rows:
                    break
                for row in rows:
                    cost = Decimal(str(row["usage"])) * unit_cost
                    evidence_id = str(uuid4())
                    # ON CONFLICT DO NOTHING：候选快照建立后、拿锁前，人工补录
                    # 或另一实例可能已为同一 attempt 提交 evidence（attempt_id
                    # 唯一约束兜底）。撞行时只跳过该行，绝不回滚整批。
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
                            args.reason,
                            str(actor["id"]),
                        ),
                    ).fetchone()
                    if inserted is None:
                        continue
                    conn.execute(
                        "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,"
                        "entity_id,metadata_json) VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            str(uuid4()),
                            str(actor["id"]),
                            "billing.evidence.append",
                            "billing_operation",
                            str(row["operation_id"]),
                            json.dumps(
                                {
                                    "evidence_id": evidence_id,
                                    "reference": reference,
                                    "reason": args.reason,
                                    "batch": "backfill_operation_costs",
                                }
                            ),
                        ),
                    )
                    backfilled += 1
                    total_cost_fen += cost

    print(f"backfilled attempts: {backfilled} total_cost_fen: {total_cost_fen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
