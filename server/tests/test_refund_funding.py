"""原充值来源退款的隔离契约；不连接客户数据库或执行真实退付。"""

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app import admin_customer_routes as routes
from app.admin_refund_funding import RefundSource, deduct_refund_source, source_commitments


def test_negative_adjustment_without_original_source_stops_before_write_envelope():
    body = routes.AdjustmentRequest(
        credits=-10,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="approval",
        reason="测试",
        confirm=True,
    )
    with (
        patch.object(routes, "_write_with_idempotency") as write,
        pytest.raises(HTTPException) as caught,
    ):
        routes.create_admin_adjustment(
            "customer", body, None, None, SimpleNamespace(user_id="operator")
        )
    assert caught.value.status_code == 422
    write.assert_not_called()


class Cursor:
    def __init__(self, row=None, rows=(), rowcount=1):
        self.row, self.rows, self.rowcount = row, list(rows), rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


def source(**changed):
    return RefundSource(
        "order-a",
        "customer",
        "PAID",
        "zpay",
        10000,
        100,
        None,
        None,
        "verified-payment",
        None,
        "2026-10-12T00:00:00Z",
        "cash-a",
        "customer",
        "CHARGE",
        100,
        0,
        "cash-a",
        "customer",
        100,
        100,
        Decimal(10000),
        "2026-10-12T00:00:00Z",
    )._replace(**changed)


def part(lot="cash-a", credits=30, lot_credits=100, amount="10000"):
    return {"lot_id": lot, "credits": credits, "lot_credits": lot_credits, "amount_fen": amount}


def operation(*, state="PENDING", budget=30, charged=0, parts=None):
    return (
        "operation",
        state,
        budget,
        charged,
        json.dumps(parts if parts is not None else [part()]),
    )


class FundingConnection:
    def __init__(self, evidence=None, *, available=None, reserved=0, other_lots=0):
        self.evidence = evidence or source()
        self.available = (
            self.evidence.lot_remaining + other_lots if available is None else available
        )
        self.reserved = reserved
        self.other_lots = other_lots
        self.operations = []
        self.legacy_times = []
        self.previous = 0
        self.ambiguous = False
        self.statements = []
        self.writes = []
        self.missing = False
        self.adjustments = []
        self.allocations = []
        self.snapshots = {}
        self.aliases = {}

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql.startswith("SELECT available_credits, reserved_credits"):
            return Cursor((self.available, self.reserved))
        if sql.startswith("SELECT ro.id"):
            assert "FOR UPDATE OF lot" in sql and params[1] == "customer"
            return Cursor(rows=[] if self.missing or params[0] != "original-a" else [self.evidence])
        if sql.startswith("SELECT 1 FROM wallet_transactions tx"):
            return Cursor((1,) if self.ambiguous else None)
        if sql.startswith("SELECT created_at FROM wallet_transactions"):
            return Cursor(rows=[(value,) for value in self.legacy_times])
        if sql.startswith("SELECT COALESCE(SUM(remaining_credits)"):
            return Cursor((self.evidence.lot_remaining + self.other_lots,))
        if sql.startswith("SELECT id,state,reserved_credits"):
            assert "FOR UPDATE" not in sql
            return Cursor(rows=self.operations)
        if sql.startswith("SELECT COALESCE(SUM(credits)"):
            return Cursor((self.previous,))
        if sql.startswith("UPDATE billing_credit_lots SET remaining_credits=remaining_credits-%s"):
            credits, lot, before, minimum = params
            assert lot == "cash-a" and before == self.evidence.lot_remaining and credits == minimum
            assert self.evidence.lot_remaining >= credits
            self.writes.append(sql)
            self.evidence = self.evidence._replace(lot_remaining=before - credits)
            return Cursor()
        if sql.startswith("SELECT pg_advisory"):
            return Cursor()
        if sql.startswith("SELECT DISTINCT ao.business_identity"):
            operation_id, identity = params
            identities = {identity, self.aliases.get(operation_id)}
            return Cursor(
                rows=[(key, *value) for key, value in self.snapshots.items() if key in identities]
            )
        if "FROM admin_adjustments aa" in sql:
            return Cursor(rows=self.adjustments)
        if sql.startswith("INSERT INTO admin_adjustment_operations "):
            identity, fingerprint, _, payload = params
            assert identity not in self.snapshots
            self.snapshots[identity] = (fingerprint, payload)
            return Cursor()
        if sql.startswith("INSERT INTO admin_adjustment_operation_keys "):
            self.aliases[params[0]] = params[1]
            return Cursor()
        if sql.startswith("SELECT 1 FROM users") or sql.startswith("SELECT 1 FROM wallets"):
            return Cursor((1,))
        if sql.lstrip().startswith("INSERT INTO wallet_transactions"):
            self.writes.append(sql)
            return Cursor()
        if sql.startswith("UPDATE wallets SET available_credits"):
            self.available += params[0]
            self.writes.append(sql)
            return Cursor((self.available,))
        if sql.lstrip().startswith("INSERT INTO admin_adjustments"):
            self.writes.append(sql)
            return Cursor()
        if sql.startswith("INSERT INTO admin_refund_allocations "):
            self.allocations.append(params)
            self.previous += params[4]
            self.writes.append(sql)
            return Cursor()
        raise AssertionError(sql)


def deduct(conn, credits=20):
    return deduct_refund_source(conn, user_id="customer", order_no="original-a", credits=credits)


@pytest.mark.parametrize(
    "changed",
    [
        {"user_id": "other"},
        {"charge_user_id": "other"},
        {"lot_user_id": "other"},
        {"status": "PENDING"},
        {"paid_at": None},
        {"provider": "admin_adjustment"},
        {"provider_trade_no": "  "},
        {"provider": "wechat_native", "transaction_id": None},
        {"package_id": "package"},
        {"package_snapshot": '{"credits":100}'},
        {"amount_fen": 0},
        {"lot_amount_fen": None},
        {"lot_amount_fen": Decimal("NaN")},
        {"charge_reserved": 1},
        {"charge_credits": 99},
        {"lot_credits": 99},
    ],
)
def test_unverified_source_never_changes_any_balance(changed):
    conn = FundingConnection(source(**changed))
    with pytest.raises(HTTPException) as caught:
        deduct(conn)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "REFUND_SOURCE_UNVERIFIED"
    assert conn.writes == []


def test_missing_or_different_order_never_uses_another_recharge():
    conn = FundingConnection(other_lots=1000)
    conn.missing = True
    with pytest.raises(HTTPException):
        deduct(conn)
    assert conn.writes == []


def test_only_selected_cash_lot_changes_wallet_is_locked_first():
    conn = FundingConnection(other_lots=250)
    result = deduct(conn, 60)
    assert conn.evidence.lot_remaining == 40
    assert conn.other_lots == 250
    assert result.lot_remaining_before == 100 and result.lot_remaining_after == 40
    assert "wallets" in conn.statements[0][0] and "FOR UPDATE" in conn.statements[0][0]
    assert "FOR UPDATE OF lot" in conn.statements[1][0]
    assert all("FOR UPDATE" not in sql for sql, _ in conn.statements if "billing_operations" in sql)
    assert all("UPDATE billing_operations" not in sql for sql, _ in conn.statements)


def test_reserved_source_cannot_be_refunded_even_with_other_wallet_balance():
    conn = FundingConnection(source(lot_remaining=70), reserved=30, other_lots=500)
    conn.operations = [operation()]
    with pytest.raises(HTTPException) as caught:
        deduct(conn, 71)
    assert caught.value.detail["code"] == "REFUND_SOURCE_BALANCE_INSUFFICIENT"
    assert conn.writes == []
    result = deduct(conn, 60)
    assert result.lot_remaining_after == 10 and conn.reserved == 30


@pytest.mark.parametrize(
    "condition", ["unallocated-refund", "wallet-drift", "source-drift", "missing-funding"]
)
def test_ambiguous_history_fails_closed_even_if_untracked_balance_hides_it(condition):
    conn = FundingConnection(source(lot_remaining=80), available=1000)
    conn.operations = [
        operation(state="SUCCEEDED", budget=20, charged=20, parts=[part(credits=20)])
    ]
    if condition == "unallocated-refund":
        conn.ambiguous = True
    if condition == "wallet-drift":
        conn.available = 79
    if condition == "source-drift":
        conn.previous = 1
    if condition == "missing-funding":
        conn.operations = []
    with pytest.raises(HTTPException) as caught:
        deduct(conn)
    assert caught.value.detail["code"] == "REFUND_SOURCE_RECONCILIATION_REQUIRED"
    assert conn.writes == []


@pytest.mark.parametrize(
    "state,charged,expected",
    [
        ("PENDING", 0, (40, 0)),
        ("SUCCEEDED", 30, (0, 20)),
        ("FAILED", 0, (0, 0)),
        ("CANCELLED", 0, (0, 0)),
    ],
)
def test_frozen_multi_source_allocations_match_actual_partial_settlement(state, charged, expected):
    row = operation(
        state=state,
        budget=70,
        charged=charged,
        parts=[part("gift", 10, 10, "0"), part(credits=40), part("cash-b", 20, 50, "5000")],
    )
    assert (
        source_commitments(
            [row], lot_id="cash-a", source_credits=100, source_amount_fen=Decimal(10000)
        )
        == expected
    )


@pytest.mark.parametrize(
    "parts",
    [
        "invalid",
        [{"credits": True}],
        [part(amount="sNaN")],
        [part(amount="Infinity")],
        [part(amount=None)],
        [part(lot_credits=90)],
        [part(credits=31)],
    ],
)
def test_malformed_funding_requires_manual_review(parts):
    row = operation(parts=parts)
    with pytest.raises(HTTPException) as caught:
        source_commitments(
            [row], lot_id="cash-a", source_credits=100, source_amount_fen=Decimal(10000)
        )
    assert caught.value.status_code == 422


def adjustment(
    conn,
    *,
    approval="approval-a",
    original="original-a",
    credits=-20,
    source_type="REFUND_APPROVAL",
    actor="operator-a",
):
    body = routes.AdjustmentRequest(
        credits=credits,
        source_document_type=source_type,
        source_document_ref=approval,
        reason="测试",
        confirm=True,
        original_recharge_order_no=original,
    )
    prices = {
        "internal_base_unit_price_fen": 1,
        "charged_unit_price_fen": 1,
        "min_recharge_fen": 1,
        "recharge_step_fen": 1,
    }
    with (
        patch.object(
            routes,
            "_write_with_idempotency",
            side_effect=lambda request, response, actor, body, business, **_: business(
                conn, "request"
            ),
        ),
        patch.object(routes, "_current_billing_snapshot", return_value=prices),
        patch.object(routes, "_infer_pricing_scope", return_value="CUSTOMER_STANDARD"),
        patch.object(routes, "_transaction_now_iso", return_value="2026-10-12T00:00:00Z"),
    ):
        return routes.create_admin_adjustment(
            "customer", body, None, None, SimpleNamespace(user_id=actor)
        )


def test_durable_refund_replay_keeps_original_allocation_and_changed_source_conflicts():
    conn = FundingConnection(other_lots=200)
    first = adjustment(conn)
    assert conn.available == 280 and conn.evidence.lot_remaining == 80
    assert len(conn.allocations) == 1
    assert first["refund_funding"]["credit_lot_id"] == "cash-a"
    assert adjustment(conn, actor="operator-b") == first
    assert conn.available == 280 and len(conn.allocations) == 1
    with pytest.raises(HTTPException) as caught:
        adjustment(conn, original="another-source")
    assert caught.value.status_code == 409 and len(conn.allocations) == 1
    adjustment(conn, approval="approval-b", credits=-30)
    assert conn.available == 250 and conn.evidence.lot_remaining == 50
    assert len(conn.allocations) == 2


def test_negative_ledger_correction_uses_same_source_contract():
    conn = FundingConnection()
    adjustment(conn, source_type="LEDGER_CORRECTION")
    assert conn.evidence.lot_remaining == 80 and len(conn.allocations) == 1


def test_old_completed_negative_cannot_acquire_invented_source_evidence():
    conn = FundingConnection()
    conn.adjustments = [("old", "测试", "old-request", 80, None, 0, None, -20)]
    with pytest.raises(HTTPException) as caught:
        adjustment(conn)
    assert caught.value.status_code == 409
    assert conn.writes == [] and conn.allocations == []


def test_injected_failure_uses_real_write_envelope_and_synthetic_transaction():
    from contextlib import contextmanager

    from fastapi import Request, Response

    from app import admin_write_contract

    conn = FundingConnection()
    events = []
    real_execute = conn.execute

    def execute(sql, params=()):
        if sql.startswith("INSERT INTO admin_write_idempotency"):
            events.append("placeholder")
            return Cursor()
        return real_execute(sql, params)

    conn.execute = execute

    @contextmanager
    def transaction():
        before = deepcopy(conn.__dict__)
        events.append("begin")
        try:
            yield conn
        except Exception:
            conn.__dict__.update(before)
            events.append("rollback")
            raise
        else:
            events.append("commit")

    body = routes.AdjustmentRequest(
        credits=-20,
        source_document_type="REFUND_APPROVAL",
        source_document_ref="approval",
        reason="测试",
        confirm=True,
        original_recharge_order_no="original-a",
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/control/customers/customer/adjustments",
            "headers": [(b"idempotency-key", b"synthetic-key")],
            "path_params": {"user_id": "customer"},
        }
    )
    prices = {
        "internal_base_unit_price_fen": 1,
        "charged_unit_price_fen": 1,
        "min_recharge_fen": 1,
        "recharge_step_fen": 1,
    }
    with (
        patch.object(admin_write_contract, "pg_transaction", transaction),
        patch.object(routes, "_current_billing_snapshot", return_value=prices),
        patch.object(routes, "_infer_pricing_scope", return_value="CUSTOMER_STANDARD"),
        patch.object(routes, "_transaction_now_iso", return_value="2026-10-12T00:00:00Z"),
        patch.object(
            routes, "record_refund_allocation", side_effect=RuntimeError("synthetic failure")
        ),
        pytest.raises(HTTPException) as caught,
    ):
        routes.create_admin_adjustment(
            "customer", body, request, Response(), SimpleNamespace(user_id="operator")
        )
    assert caught.value.status_code == 503
    assert events == ["begin", "placeholder", "rollback"]
    assert conn.evidence.lot_remaining == 100 and conn.available == 100
    assert conn.allocations == [] and conn.snapshots == {}


class SettlementConnection(FundingConnection):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.billing = {}

    def execute(self, sql, params=()):
        if sql.startswith("SELECT * FROM billing_operations"):
            return Cursor(self.billing)
        if sql.startswith("SELECT parent_user_id"):
            return Cursor((None,))
        if sql.startswith("SELECT user_id FROM wallets"):
            return Cursor(("customer",))
        if sql.startswith("UPDATE billing_credit_lots SET remaining_credits=remaining_credits+%s"):
            assert params[1] == "cash-a"
            self.evidence = self.evidence._replace(
                lot_remaining=self.evidence.lot_remaining + params[0]
            )
            return Cursor()
        if sql.startswith("UPDATE wallets SET available_credits=available_credits+%s,"):
            self.available += params[0]
            self.reserved -= params[1]
            return Cursor()
        if sql.startswith("UPDATE billing_operations SET state="):
            state, units, charged, revenue, nominal, _ = params
            self.billing.update(
                state=state,
                actual_units=units,
                charged_credits=charged,
                revenue_fen=revenue,
                nominal_revenue_fen=nominal,
            )
            self.operations = [
                (
                    "operation",
                    state,
                    self.billing["reserved_credits"],
                    charged,
                    self.billing["funding_json"],
                )
            ]
            return Cursor()
        return super().execute(sql, params)


def settlement_snapshot():
    return {
        "enabled": True,
        "unit_credits": "1",
        "unit_rounding": "exact",
        "discount_basis_points": 10000,
        "consumption_rounding": "exact",
        "points_per_yuan": 100,
    }


def seed_settlement(conn, *, parts, reserved):
    conn.billing = {
        "id": "operation",
        "user_id": "customer",
        "state": "PENDING",
        "reserved_credits": reserved,
        "charged_credits": 0,
        "billing_round": 1,
        "pricing_snapshot_json": json.dumps(settlement_snapshot()),
        "funding_json": json.dumps(parts),
    }
    conn.operations = [operation(budget=reserved, parts=parts)]


def test_partial_reservation_refund_then_actual_settlement_releases_to_same_source():
    from app.usage_billing import finish_operation

    conn = SettlementConnection(evidence=source(lot_remaining=70), reserved=30)
    seed_settlement(conn, parts=[part(credits=30)], reserved=30)
    adjustment(conn, credits=-60)
    assert (conn.available, conn.reserved, conn.evidence.lot_remaining) == (10, 30, 10)
    assert finish_operation(conn, operation_id="operation", units=10, succeeded=True) == 10
    assert (conn.available, conn.reserved, conn.evidence.lot_remaining) == (30, 0, 30)
    assert conn.billing["revenue_fen"] == Decimal(1000)
    adjustment(conn, approval="second-refund", credits=-30)
    assert conn.evidence.lot_remaining == 0 and conn.available == 0
    assert conn.previous == 90 and conn.billing["charged_credits"] == 10


def test_refunded_cash_is_not_available_to_later_gift_funded_revenue():
    from app.usage_billing import finish_operation

    conn = SettlementConnection(other_lots=100)
    adjustment(conn, credits=-60)
    assert conn.evidence.lot_remaining == 40 and conn.other_lots == 100
    # 按受理时已经冻结的分配消费现金40及赠送60；不把退款后的现金60再次计入收入。
    conn.evidence = conn.evidence._replace(lot_remaining=0)
    conn.other_lots = 40
    conn.available = 40
    conn.reserved = 100
    seed_settlement(conn, parts=[part(credits=40), part("gift", 60, 100, "0")], reserved=100)
    finish_operation(conn, operation_id="operation", units=100, succeeded=True)
    assert conn.billing["revenue_fen"] == Decimal(4000)
    assert conn.available == 40 and conn.reserved == 0 and conn.other_lots == 40
    assert conn.previous == 60


def test_unverified_historical_execution_cannot_be_newly_registered():
    from app import admin_refund_routes as refunds

    writes = []

    class Conn:
        def execute(self, sql, params=()):
            if sql.startswith("SELECT id, target_user_id"):
                return Cursor(("old", "customer", "REFUND_APPROVAL"))
            if "REFUND" in sql and "FROM admin_adjustments aa" in sql:
                return Cursor(None if "admin_refund_allocations proof" in sql else (1,))
            if sql.startswith("SELECT 1 FROM refund_executions"):
                return Cursor(None)
            writes.append(sql)
            return Cursor()

    body = refunds.RefundExecutionRequest(
        channel="other", provider_ref="synthetic", reason="测试", confirm=True
    )
    with (
        patch.object(
            refunds,
            "_write_with_idempotency",
            side_effect=lambda request, response, actor, body, business, **_: business(
                Conn(), "request"
            ),
        ),
        pytest.raises(HTTPException) as caught,
    ):
        refunds.mark_refund_execution("old", body, None, None, SimpleNamespace(user_id="operator"))
    assert caught.value.detail["code"] == "REFUND_SOURCE_REVIEW_REQUIRED"
    assert writes == []


def test_new_source_refund_endpoint_requires_negative_source_bound_request():
    paths = {route.path for route in routes.router.routes}
    assert "/api/control/customers/{user_id}/source-refunds" in paths
    body = routes.AdjustmentRequest(
        credits=20,
        source_document_type="FREE_GRANT",
        source_document_ref="approval",
        reason="测试",
        confirm=True,
    )
    with patch.object(routes, "create_admin_adjustment") as legacy, pytest.raises(HTTPException):
        routes.create_source_refund(
            "customer", body, None, None, SimpleNamespace(user_id="operator")
        )
    legacy.assert_not_called()
    negative = body.model_copy(
        update={
            "credits": -20,
            "source_document_type": "REFUND_APPROVAL",
            "original_recharge_order_no": "original-a",
        }
    )
    with patch.object(
        routes, "create_admin_adjustment", return_value={"verified": True}
    ) as validated:
        assert routes.create_source_refund(
            "customer", negative, None, None, SimpleNamespace(user_id="operator")
        ) == {"verified": True}
    assert validated.call_args.args[1].original_recharge_order_no == "original-a"


@pytest.mark.parametrize(
    "timestamp",
    ["invalid", "2026-10-11T00:00:00", "2026-10-12T00:00:00Z", "2026-10-12T01:00:00+00:00"],
)
def test_legacy_consumption_timestamps_must_prove_no_source_overlap(timestamp):
    conn = FundingConnection()
    conn.legacy_times = [timestamp]
    with pytest.raises(HTTPException) as caught:
        deduct(conn)
    assert caught.value.detail["code"] == "REFUND_SOURCE_RECONCILIATION_REQUIRED"
    assert conn.writes == []


def test_provably_earlier_legacy_consumption_does_not_block_new_cash_source():
    conn = FundingConnection()
    conn.legacy_times = ["2026-10-11T23:00:00+00:00"]
    assert deduct(conn).lot_remaining_after == 80
