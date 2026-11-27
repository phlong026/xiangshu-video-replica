"""经营核对工单与收入凭证，不改写原始计费记录。"""

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.admin_cash_evidence import CASH_ORDER_ELIGIBLE, NONCASH_ADJUSTMENT
from app.admin_customer_metrics import utc_text_timestamp
from app.admin_write_contract import http_error
from app.billing_reports import UNMETERED_DELIVERED_CHARGE, date_bounds
from app.db_portable import BusinessConnection, BusinessRow
from app.usage_billing import resolve_wallet_owner

ReviewKind = Literal["cost", "revenue", "legacy_settlement"]
ReviewState = Literal["evidence", "review", "verified", "unverifiable"]
ReviewAge = Literal["0-7", "8-30", "31-60", "61-90", "over-90"]

_UNMETERED = UNMETERED_DELIVERED_CHARGE.format(calls="calls.attempt_count")
_COST_UNKNOWN = f"(COALESCE(calls.has_unknown, false) OR {_UNMETERED})"
_AGED = {
    "0-7": (0, 7),
    "8-30": (8, 30),
    "31-60": (31, 60),
    "61-90": (61, 90),
    "over-90": (91, 3652059),
}
_CALLS = """
    SELECT operation_id, count(*) AS attempt_count,
           bool_or(effective_cost_fen IS NULL) AS has_unknown,
           bool_or(effective_cost_fen IS NULL AND state='PENDING') AS pending,
           bool_or(effective_cost_fen IS NULL AND usage IS NULL) AS missing_usage,
           bool_or(effective_cost_fen IS NULL AND unit_cost_fen IS NULL) AS missing_price
    FROM billing_effective_attempts GROUP BY operation_id
"""


class FundingPart(BaseModel):
    lot_id: str | None
    credits: int = Field(gt=0)
    lot_credits: int = Field(gt=0)
    amount_fen: Decimal | None = Field(ge=0)


_FUNDING = TypeAdapter(list[FundingPart])


def list_reviews(
    conn: BusinessConnection,
    *,
    start: date,
    end: date,
    kind: ReviewKind,
    state: ReviewState | None,
    age: ReviewAge | None,
    reason_code: str | None,
    owner: str | None,
    subject_ids: list[str],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    lower, upper = date_bounds(start, end)
    scope_values: tuple[Any, ...] = (subject_ids,) if subject_ids else (lower, upper)
    age_min, age_max = _AGED[age] if age else (None, None)
    if kind == "legacy_settlement":
        occurred = utc_text_timestamp("t.created_at")
        scope = "t.id=ANY(%s)" if subject_ids else f"{occurred}>=%s AND {occurred}<%s"
        facts = f"""
            SELECT t.id AS subject_id, 'legacy_settlement' AS kind, t.user_id,
                   'legacy_settlement' AS service, {occurred} AS occurred_at,
                   'historical_unlinked' AS reason_code, true AS needs_evidence
            FROM wallet_transactions t
            WHERE t.type='SETTLE' AND t.billing_operation_id IS NULL
              AND {scope}
        """
        values = scope_values
    else:
        scope = "o.id=ANY(%s)" if subject_ids else "o.completed_at>=%s AND o.completed_at<%s"
        facts = f"""
            SELECT o.id AS subject_id, r.kind, o.user_id, o.service, o.completed_at AS occurred_at,
                   r.reason_code, r.needs_evidence
            FROM billing_effective_operations o
            LEFT JOIN ({_CALLS}) calls ON calls.operation_id=o.id
            CROSS JOIN LATERAL (VALUES
                ('cost', {_COST_UNKNOWN},
                    CASE WHEN {_UNMETERED} THEN 'missing_attempt'
                         WHEN calls.pending THEN 'attempt_pending'
                         WHEN calls.missing_usage THEN 'missing_usage'
                         WHEN calls.missing_price THEN 'missing_price'
                         ELSE 'cost_evidence' END),
                ('revenue', o.effective_revenue_fen IS NULL, 'missing_funding')
            ) r(kind, needs_evidence, reason_code)
            WHERE o.state<>'PENDING' AND o.completed_at IS NOT NULL AND {scope}
              AND r.kind=%s
        """
        values = (*scope_values, kind)
    rows = conn.execute(
        f"""
        WITH facts AS ({facts}), scoped AS (
          SELECT facts.subject_id, facts.kind, facts.user_id, facts.service, facts.occurred_at,
                 facts.reason_code, (facts.needs_evidence AND NOT (facts.kind='legacy_settlement'
                   AND review.state IS NOT DISTINCT FROM 'verified')) AS needs_evidence,
                 COALESCE(customer.display_name, customer.username) AS customer,
                 COALESCE(review.state,'evidence') AS state,
                 review.owner_id, owner.username AS owner,
                 review.next_step, review.next_review_on, review.evidence_ref,
                 reviewer.username AS reviewed_by, review.updated_at AS last_checked_at,
                 COALESCE(review.version,0) AS version,
                 GREATEST(0,(now() AT TIME ZONE 'Asia/Shanghai')::date
                   - (facts.occurred_at AT TIME ZONE 'Asia/Shanghai')::date) AS age_days
          FROM facts
          LEFT JOIN billing_review_cases review
            ON review.kind=facts.kind AND review.subject_id=facts.subject_id
          LEFT JOIN users customer ON customer.id=facts.user_id
          LEFT JOIN users owner ON owner.id=review.owner_id
          LEFT JOIN users reviewer ON reviewer.id=review.reviewed_by
          WHERE facts.needs_evidence OR review.subject_id IS NOT NULL
        ), matched AS (
          SELECT * FROM scoped
          WHERE (%s::text IS NULL OR state=%s)
            AND (%s::int IS NULL OR age_days BETWEEN %s AND %s)
            AND (%s::text IS NULL OR reason_code=%s)
            AND (%s::text IS NULL OR
              (%s='unassigned' AND owner_id IS NULL) OR
              (%s<>'unassigned' AND lower(owner)=lower(%s)))
        ), page AS (
          SELECT * FROM matched ORDER BY occurred_at ASC, subject_id
          LIMIT %s OFFSET %s
        )
        SELECT to_jsonb(page) AS item, totals.total,
               totals.without_owner, totals.overdue
        FROM (SELECT count(*) AS total,
                     count(*) FILTER (WHERE owner_id IS NULL AND state<>'verified')
                       AS without_owner,
                     count(*) FILTER (WHERE age_days>30 AND state<>'verified') AS overdue
              FROM matched) totals
        LEFT JOIN page ON true
        ORDER BY page.occurred_at, page.subject_id
        """,  # noqa: S608 - 只有固定事实查询、列和日期表达式
        (
            *values,
            state,
            state,
            age_min,
            age_min,
            age_max,
            reason_code,
            reason_code,
            owner,
            owner,
            owner,
            owner,
            limit,
            offset,
        ),
    ).fetchall()
    return {
        "items": [row["item"] for row in rows if row["item"] is not None],
        "total": int(rows[0]["total"]),
        "without_owner": int(rows[0]["without_owner"]),
        "overdue": int(rows[0]["overdue"]),
    }


def _verified_revenue(
    conn: BusinessConnection, operation: BusinessRow
) -> tuple[Decimal, list[dict[str, Any]]]:
    """用已留存的充值/赠送凭证重建消耗收入，未追溯的余额仍待核对。"""
    try:
        funding = _FUNDING.validate_json(str(operation["funding_json"]))
    except ValidationError as exc:
        raise http_error(
            409, "REVENUE_PROOF_REQUIRED", "这笔积分缺少原充值或赠送凭证，请保留待核对。"
        ) from exc
    remaining = int(operation["charged_credits"])
    if remaining <= 0:
        raise http_error(
            409, "REVENUE_PROOF_REQUIRED", "找不到这笔业务对应的扣分记录，请保留待核对。"
        )
    # 子账号可能换过主账号，优先使用业务冻结时的实际付款钱包。
    reserved_wallets = conn.execute(
        "SELECT DISTINCT user_id FROM wallet_transactions "
        "WHERE billing_operation_id=%s AND type='RESERVE' LIMIT 2",
        (operation["id"],),
    ).fetchall()
    if operation["user_id"] is None or len(reserved_wallets) > 1:
        raise http_error(
            409, "REVENUE_PROOF_REQUIRED", "这笔扣分的付款钱包无法确认，请保留待核对。"
        )
    wallet_owner = (
        str(reserved_wallets[0]["user_id"])
        if reserved_wallets
        else resolve_wallet_owner(conn, str(operation["user_id"]))
    )
    lots = [part.lot_id for part in funding if part.lot_id is not None]
    rows = conn.execute(
        f"""
        SELECT lot.id, lot.credits, ro.merchant_order_no, ro.amount_fen,
               CASE WHEN {NONCASH_ADJUSTMENT} OR ro.amount_fen=0 THEN 'grant'
                    WHEN {CASH_ORDER_ELIGIBLE} THEN 'cash' END AS origin
        FROM billing_credit_lots lot
        JOIN wallet_transactions tx ON tx.id=lot.id AND tx.type='CHARGE'
          AND tx.user_id=lot.user_id AND tx.available_delta=lot.credits
        JOIN recharge_orders ro ON ro.id=tx.recharge_order_id AND ro.user_id=lot.user_id
          AND ro.credits=lot.credits AND ro.status='PAID' AND ro.paid_at IS NOT NULL
        WHERE lot.id=ANY(%s) AND lot.user_id=%s
        FOR SHARE OF ro
        """,  # noqa: S608 - 使用现有固定的实收与赠送口径
        (lots, wallet_owner),
    ).fetchall()
    receipts = {row["id"]: row for row in rows}
    proofs: list[dict[str, Any]] = []
    result = Decimal(0)
    for part in funding:
        used = min(remaining, part.credits)
        if used <= 0:
            continue
        lot_credits = part.lot_credits
        if lot_credits < used:
            break
        receipt = receipts.get(part.lot_id)
        if part.amount_fen is not None:
            amount = part.amount_fen
            origin = "snapshot"
        elif receipt is not None and receipt["origin"] and lot_credits == receipt["credits"]:
            origin = str(receipt["origin"])
            amount = Decimal(0) if origin == "grant" else Decimal(receipt["amount_fen"])
        else:
            break
        result += amount * used / lot_credits
        proofs.append(
            {
                "lot_id": part.lot_id,
                "credits_used": used,
                "source": origin,
                "amount_fen": str(amount),
                "order_no": None if receipt is None else receipt["merchant_order_no"],
            }
        )
        remaining -= used
    if remaining:
        raise http_error(
            409, "REVENUE_PROOF_REQUIRED", "仍有积分找不到原充值或赠送凭证，请保留待核对。"
        )
    return result.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), proofs


def save_review(
    conn: BusinessConnection,
    *,
    subject_id: str,
    kind: ReviewKind,
    state: ReviewState,
    owner: str,
    actor_id: str,
    next_step: str,
    next_review_on: date | None,
    evidence_ref: str,
    expected_version: int,
    require_unconfirmed_revenue: bool = False,
) -> dict[str, object]:
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("billing:evidence:" + subject_id,))
    case = conn.execute(
        "SELECT version FROM billing_review_cases WHERE kind=%s AND subject_id=%s",
        (kind, subject_id),
    ).fetchone()
    if expected_version != (case["version"] if case else 0):
        raise http_error(409, "BILLING_REVIEW_STALE", "记录已被更新，请刷新后重试。")
    handler = conn.execute(
        "SELECT id FROM users WHERE lower(username)=lower(%s) AND role='admin' AND is_active=1",
        (owner,),
    ).fetchone()
    if handler is None:
        raise http_error(422, "BILLING_REVIEW_OWNER_INVALID", "请输入有效的管理员账号作为负责人。")
    if kind == "legacy_settlement":
        operation = conn.execute(
            """
            SELECT t.id, (
              (g.status='SUCCEEDED' AND g.quality_status IN
                 ('AUDIO_OK','NOT_REQUIRED','REPLICA_REVIEW_PASSED')
               AND ((g.archive_status='DIRECT'
                     AND NULLIF(trim(g.provider_result_url),'') IS NOT NULL)
                 OR (g.archive_status='ARCHIVED'
                     AND NULLIF(trim(video_asset.storage_uri),'') IS NOT NULL)))
              OR (oral.status='SUCCEEDED' AND NULLIF(trim(oral_asset.storage_uri),'') IS NOT NULL)
            ) AND EXISTS (
              SELECT 1 FROM wallet_transactions m
              WHERE m.user_id=t.user_id AND m.billing_round=t.billing_round
                AND m.billing_operation_id IS NULL AND m.type IN ('RESERVE','SETTLE','RELEASE')
                AND ((t.task_id IS NOT NULL AND m.task_id=t.task_id)
                  OR (t.oral_task_id IS NOT NULL AND m.oral_task_id=t.oral_task_id))
              HAVING count(*) FILTER (WHERE m.type='RESERVE')=1
                AND count(*) FILTER (WHERE m.type='SETTLE')=1 AND sum(m.reserved_delta)=0
            ) AS verified_source
            FROM wallet_transactions t
            LEFT JOIN generation_tasks g ON g.id=t.task_id
            LEFT JOIN oral_tasks oral ON oral.id=t.oral_task_id
            LEFT JOIN assets video_asset ON video_asset.id=g.result_asset_id
            LEFT JOIN assets oral_asset ON oral_asset.id=oral.result_asset_id
            WHERE t.id=%s AND t.type='SETTLE' AND t.billing_operation_id IS NULL
            """,
            (subject_id,),
        ).fetchone()
    elif kind == "revenue":
        operation = conn.execute(
            "SELECT * FROM billing_effective_operations WHERE id=%s AND state<>'PENDING'",
            (subject_id,),
        ).fetchone()
    else:
        operation = conn.execute(
            f"""
            SELECT o.*, {_COST_UNKNOWN} AS unknown_cost
            FROM billing_effective_operations o
            LEFT JOIN ({_CALLS}) calls ON calls.operation_id=o.id
            WHERE o.id=%s AND o.state<>'PENDING'
            """,  # noqa: S608 - 固定计费口径
            (subject_id,),
        ).fetchone()
    if operation is None:
        raise http_error(404, "BILLING_REVIEW_NOT_FOUND", "未找到可核对的账务记录。")
    if require_unconfirmed_revenue and operation["effective_revenue_fen"] is not None:
        raise http_error(409, "BILLING_REVIEW_ALREADY_COMPLETE", "这笔收入已经确认，请刷新清单。")
    if kind != "legacy_settlement" and case is None and state != "verified":
        missing = (
            operation["unknown_cost"]
            if kind == "cost"
            else operation["effective_revenue_fen"] is None
        )
        if not missing:
            raise http_error(
                409, "BILLING_REVIEW_ALREADY_COMPLETE", "这笔业务已有凭证，请刷新清单。"
            )
    confirmed_amount: Decimal | None = None
    if state == "verified":
        if not evidence_ref:
            raise http_error(422, "BILLING_REVIEW_REFERENCE_REQUIRED", "请填写凭证编号。")
        if kind == "legacy_settlement" and not operation["verified_source"]:
            raise http_error(
                409, "BILLING_REVIEW_EVIDENCE_REQUIRED", "请核实原任务、交付结果和冻结扣分流水。"
            )
        if kind == "cost" and operation["unknown_cost"]:
            raise http_error(
                409, "BILLING_REVIEW_EVIDENCE_REQUIRED", "请先补齐原始账务凭证，再确认核对。"
            )
        if kind == "revenue" and operation["effective_revenue_fen"] is None:
            confirmed_amount, proofs = _verified_revenue(conn, operation)
            conn.execute(
                "INSERT INTO billing_revenue_confirmations "
                "(operation_id,revenue_fen,proof_json,reference,actor_user_id) "
                "VALUES (%s,%s,%s::jsonb,%s,%s)",
                (subject_id, confirmed_amount, json.dumps(proofs), evidence_ref, actor_id),
            )

    version = expected_version + 1
    conn.execute(
        """
        INSERT INTO billing_review_cases
          (subject_id,kind,state,owner_id,next_step,next_review_on,evidence_ref,reviewed_by,version)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (kind,subject_id) DO UPDATE SET
          state=EXCLUDED.state, owner_id=EXCLUDED.owner_id, next_step=EXCLUDED.next_step,
          next_review_on=EXCLUDED.next_review_on, evidence_ref=EXCLUDED.evidence_ref,
          reviewed_by=EXCLUDED.reviewed_by, version=EXCLUDED.version, updated_at=now()
        """,
        (
            subject_id,
            kind,
            state,
            handler["id"],
            next_step,
            next_review_on,
            evidence_ref,
            actor_id if state == "verified" else None,
            version,
        ),
    )
    return {
        "subject_id": subject_id,
        "kind": kind,
        "state": state,
        "version": version,
        "confirmed_revenue_fen": None if confirmed_amount is None else str(confirmed_amount),
    }
