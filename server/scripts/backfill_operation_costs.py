"""批量补录历史 UNKNOWN 调用成本（审计修复 2026-11-15，模块 M2）。

背景：quality_inspection / analysis_repair / analysis_repair_deepseek 三个
内部科目的计费目录长期未配单价，begin_attempt 快照到的 unit_cost_fen 为空，
complete_attempt 把这些调用记成 state=UNKNOWN、cost_fen=NULL——每个月堆积
数百条"成本待核对"，人工在成本核对页逐条补录根本追不上，月度毛利率因此
长期"待核对"。

写入仅针对逐条核过的调用，按原始凭证上的历史费率补记 billing_evidence。
已确认成本的调用幂等跳过，缺用量的调用保留待核对。

用法（默认 dry-run，只打印规模；加 --apply 才写库）::

    # 先看规模
    python -m scripts.backfill_operation_costs --service quality_inspection \
        --actor-user-id <uuid> --reason "排查有用量但缺历史成本的调用"
    # 确认后落库
    python -m scripts.backfill_operation_costs --service quality_inspection \
        --actor-user-id <uuid> --reason "核对历史账单" --apply \
        --attempt-id <uuid> --reference <账单编号> --verified-unit-cost-fen <账单单价>

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
from app.db_portable import BusinessConnection, BusinessRow

# 事务批量：每批一个提交，批内写一条审计；中断时已提交批次各自有审计可查。
_BATCH_SIZE = 500


def _candidates(
    conn: BusinessConnection, *, service: str, attempt_ids: list[str] | None
) -> dict[str, int]:
    """统计口径与成本核对页一致：成本未知的已完成调用。

    各桶条件写在 FILTER 里而不是外层 WHERE——外层过滤会让 pending 桶恒为 0，
    运营看不到"还有多少在途请求将来才需要补"。
    """
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE o.state <> 'PENDING' AND a.state = 'UNKNOWN'
                             AND a.cost_fen IS NULL AND e.attempt_id IS NULL
                             AND a.usage IS NOT NULL) AS backfillable,
          count(*) FILTER (WHERE o.state <> 'PENDING' AND a.state = 'UNKNOWN'
                             AND a.cost_fen IS NULL AND e.attempt_id IS NULL
                             AND a.usage IS NULL) AS unmetered,
          count(*) FILTER (WHERE o.state = 'PENDING') AS pending,
          count(*) FILTER (WHERE o.state <> 'PENDING') AS total_finished
        FROM billing_attempts a
        JOIN billing_operations o ON o.id = a.operation_id
        LEFT JOIN billing_evidence e ON e.attempt_id = a.id
        WHERE a.service = %s AND (%s::text[] IS NULL OR a.id=ANY(%s))
        """,
        (service, attempt_ids, attempt_ids),
    ).fetchone()
    return {
        "backfillable": int(row["backfillable"]),
        "unmetered": int(row["unmetered"]),
        "pending": int(row["pending"]),
        "total_finished": int(row["total_finished"]),
    }


def _fetch_batch(
    conn: BusinessConnection, *, service: str, attempt_ids: list[str], limit: int
) -> list[BusinessRow]:
    return conn.execute(
        """
        SELECT a.id AS attempt_id, a.operation_id, a.usage
        FROM billing_attempts a
        JOIN billing_operations o ON o.id = a.operation_id
        LEFT JOIN billing_evidence e ON e.attempt_id = a.id
        WHERE a.service = %s
          AND a.id=ANY(%s)
          AND a.state = 'UNKNOWN'
          AND a.cost_fen IS NULL
          AND e.attempt_id IS NULL
          AND a.usage IS NOT NULL
          AND o.state <> 'PENDING'
        ORDER BY a.completed_at, a.id
        LIMIT %s
        FOR UPDATE OF a SKIP LOCKED
        """,  # noqa: S608 - 固定列清单
        (service, attempt_ids, limit),
    ).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补录已逐条核实用量和历史费率的成本。")
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
        help="核对历史调用的账单或费率凭证编号",
    )
    parser.add_argument(
        "--attempt-id", action="append", default=[], help="已核实的调用编号，可重复"
    )
    parser.add_argument("--verified-unit-cost-fen", type=Decimal, help="凭证确认的历史单价（分）")
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
    verified_price: Decimal | None = args.verified_unit_cost_fen
    if verified_price is not None and (not verified_price.is_finite() or verified_price < 0):
        parser.error("--verified-unit-cost-fen 必须为有限非负数")
    if args.apply and (verified_price is None or not args.reference.strip() or not args.attempt_id):
        parser.error("--apply 必须提供 --attempt-id、--reference 和 --verified-unit-cost-fen")

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
                "SELECT id FROM users WHERE id=%s AND role='admin' AND is_active=1",
                (args.actor_user_id,),
            ).fetchone()
            if actor is None:
                print(
                    f"error: --actor-user-id {args.actor_user_id!r} is not a known user",
                    file=sys.stderr,
                )
                return 1
            tariff = read_tariff(conn, args.service)
            stats = _candidates(conn, service=args.service, attempt_ids=args.attempt_id or None)
            catalog_price = tariff.unit_cost_fen if tariff else None
            print(
                f"service={args.service} current_catalog_cost={catalog_price} "
                f"verified_historical_unit_cost={verified_price} "
                f"backfillable={stats['backfillable']} unmetered={stats['unmetered']} "
                f"pending={stats['pending']} total_finished={stats['total_finished']}"
            )
            if not args.apply:
                # 只读模式（默认）：打印规模即退出。
                return 0
        assert verified_price is not None
        if stats["backfillable"] == 0:
            print("nothing to backfill")
            return 0

        reference = args.reference.strip()
        backfilled = 0
        total_cost_fen = Decimal(0)
        while backfilled < args.limit:
            # 每批独立提交：批内锁定行（FOR UPDATE SKIP LOCKED）+ 写证据 + 写审计；
            # 中断时已提交批次各自带审计，重跑幂等（已确认的调用会被查询排除）。
            with raw.transaction():
                rows = _fetch_batch(
                    conn,
                    service=args.service,
                    attempt_ids=args.attempt_id,
                    limit=min(_BATCH_SIZE, args.limit - backfilled),
                )
                if not rows:
                    break
                for row in rows:
                    cost = Decimal(str(row["usage"])) * verified_price
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
