"""客户资金整改的防御性函数契约；不启动认证或真实资金入口。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

APP = Path(__file__).resolve().parents[1] / "app"


class Cursor:
    def __init__(self, row=None, rows=None, rowcount=1):
        self.row, self.rows, self.rowcount = row, rows or [], rowcount

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class CustomerTargetTests(unittest.TestCase):
    def test_customer_target_guard_rejects_team_roles_and_super_flags(self):
        from app import admin_customer_routes

        guard = getattr(admin_customer_routes, "require_customer_target", None)
        self.assertIsNotNone(guard, "客户写入口需要共用的事务内目标校验")
        for role, super_flag, activated in [
            ("admin", 0, True),
            ("auditor", 0, True),
            ("admin", 1, True),
            ("customer", 1, True),
            ("employee", 0, False),
        ]:
            conn = SimpleNamespace(execute=lambda *_: Cursor((1, role, super_flag, activated)))
            with self.assertRaises(HTTPException) as caught:
                guard(conn, "target")
            self.assertEqual(caught.exception.status_code, 404)
        for role, activated in [("customer", False), ("employee", True)]:
            conn = SimpleNamespace(execute=lambda *_: Cursor((1, role, 0, activated)))
            self.assertEqual(guard(conn, "customer"), 1)

    def test_customer_status_and_annotation_call_shared_guard(self):
        for filename, names in [
            ("admin_customer_routes.py", {"suspend_customer", "resume_customer"}),
            ("admin_customer_annotation_routes.py", {"_require_customer"}),
        ]:
            tree = ast.parse((APP / filename).read_text())
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in names:
                    self.assertTrue(
                        any(
                            isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "require_customer_target"
                            for call in ast.walk(node)
                        ),
                        node.name,
                    )


class RefundDirectionTests(unittest.TestCase):
    def test_execution_requires_actual_negative_refund_fact_before_any_write(self):
        writes = []

        class Conn:
            def execute(self, sql, params=()):
                if sql.startswith("SELECT id, target_user_id"):
                    return Cursor(("a", "customer", "REFUND_APPROVAL"))
                if "FROM admin_adjustments aa" in sql:
                    return Cursor(None)
                if sql.startswith("SELECT 1 FROM refund_executions"):
                    return Cursor(None)
                writes.append(sql)
                return Cursor()

        from app import admin_refund_routes

        body = SimpleNamespace(
            channel="other", provider_ref="synthetic-ref", note="", reason="测试"
        )
        with (
            patch.object(
                admin_refund_routes,
                "_write_with_idempotency",
                side_effect=lambda request, response, actor, body, business, **_: business(
                    Conn(), "request"
                ),
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            admin_refund_routes.mark_refund_execution(
                "a", body, None, None, SimpleNamespace(user_id="operator")
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(writes, [])

    def test_list_and_count_share_refund_fact_join(self):
        source = (APP / "admin_refund_routes.py").read_text()
        tree = ast.parse(source)
        method = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "list_refunds"
        )
        self.assertGreaterEqual(
            ast.get_source_segment(source, method).count("{REFUND_FACT_JOIN}"), 2
        )


class DurableAdjustmentTests(unittest.TestCase):
    def test_identity_is_customer_scoped_and_directional(self):
        from app import admin_customer_routes

        identity = getattr(admin_customer_routes, "_adjustment_identity", None)
        self.assertIsNotNone(identity, "调账需要独立于操作者和请求键的稳定业务身份")
        self.assertEqual(
            identity("a", "FREE_GRANT", "source", 100), identity("a", "FREE_GRANT", "source", 200)
        )
        self.assertNotEqual(
            identity("a", "FREE_GRANT", "source", 100), identity("b", "FREE_GRANT", "source", 100)
        )
        self.assertNotEqual(
            identity("a", "REFUND_APPROVAL", "source", -100),
            identity("a", "REFUND_APPROVAL", "source", 100),
        )


class OperationMemoryConnection:
    """只模拟业务身份存储；资金和 PostgreSQL 锁语义另由集成用例验证。"""

    def __init__(self):
        self.operations = {}
        self.aliases = {}
        self.legacy = []
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if sql.startswith("SELECT pg_advisory"):
            return Cursor()
        if sql.startswith("SELECT DISTINCT ao.business_identity"):
            operation_id, identity = params
            identities = {identity, self.aliases.get(operation_id)}
            return Cursor(
                rows=[(key, *value) for key, value in self.operations.items() if key in identities]
            )
        if sql.startswith("INSERT INTO admin_adjustment_operations "):
            identity, fingerprint, _, response_body = params
            assert identity not in self.operations
            self.operations[identity] = (fingerprint, response_body)
            return Cursor()
        if sql.startswith("INSERT INTO admin_adjustment_operation_keys "):
            operation_id, identity = params
            if operation_id in self.aliases:
                assert self.aliases[operation_id] == identity
            self.aliases[operation_id] = identity
            return Cursor()
        if "FROM admin_adjustments aa" in sql:
            return Cursor(rows=self.legacy)
        raise AssertionError(sql)


class DurableReplayTests(unittest.TestCase):
    def setUp(self):
        from app import admin_customer_routes as routes

        self.routes = routes
        self.conn = OperationMemoryConnection()
        self.payload = {
            "adjustment_id": "adjustment-a",
            "wallet_balance_after": 100,
            "request_id": "first-request",
        }

    def args(self, *, operation="operation-a", customer="a", credits=100, reason="赠送"):
        identity = self.routes._adjustment_identity(customer, "FREE_GRANT", "source-one", credits)
        return {
            "business_identity": identity,
            "operation_id": operation,
            "fingerprint": self.routes.business_request_hash(
                {"target_user_id": customer, "credits": credits, "reason": reason}
            ),
            "user_id": customer,
            "source_document_type": "FREE_GRANT",
            "source_document_ref": "source-one",
            "credits": credits,
            "reason": reason,
        }

    def record(self, **kwargs):
        args = self.args(**kwargs)
        self.routes._record_adjustment_operation(
            self.conn,
            **{key: args[key] for key in ("business_identity", "operation_id", "fingerprint")},
            payload=self.payload,
        )

    def test_response_loss_and_new_operator_request_replay_original_result(self):
        self.assertIsNone(self.routes._replay_adjustment_operation(self.conn, **self.args()))
        self.record()
        self.assertEqual(
            self.routes._replay_adjustment_operation(self.conn, **self.args()), self.payload
        )
        self.assertEqual(
            self.routes._replay_adjustment_operation(
                self.conn, **self.args(operation="operator-b-new-id")
            ),
            self.payload,
        )
        self.assertEqual(len(self.conn.operations), 1)
        self.assertEqual(len(self.conn.aliases), 2)

    def test_content_conflict_has_no_new_snapshot(self):
        self.record()
        for changed in ({"credits": 200}, {"reason": "更改原因"}):
            with self.assertRaises(HTTPException) as caught:
                self.routes._replay_adjustment_operation(
                    self.conn, **self.args(operation="new-id", **changed)
                )
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.conn.operations), 1)
        self.assertEqual(len(self.conn.aliases), 1)

    def test_one_source_many_customers_and_id_reuse_conflict(self):
        self.record()
        self.assertIsNone(
            self.routes._replay_adjustment_operation(
                self.conn, **self.args(operation="operation-b", customer="b")
            )
        )
        with self.assertRaises(HTTPException) as caught:
            self.routes._replay_adjustment_operation(self.conn, **self.args(customer="b"))
        self.assertEqual(caught.exception.status_code, 409)

    def test_replayed_alias_cannot_be_reassigned_to_another_customer(self):
        self.record()
        self.routes._replay_adjustment_operation(self.conn, **self.args(operation="alias"))
        with self.assertRaises(HTTPException):
            self.routes._replay_adjustment_operation(
                self.conn, **self.args(operation="alias", customer="b")
            )

    def test_matching_historical_fact_gets_only_append_only_identity(self):
        self.conn.legacy = [
            ("legacy-a", "赠送", "legacy-request", 100, "order-a", 0, "CUSTOMER_STANDARD", 100)
        ]
        result = self.routes._replay_adjustment_operation(self.conn, **self.args())
        self.assertEqual(result["adjustment_id"], "legacy-a")
        self.assertEqual(result["request_id"], "legacy-request")
        self.assertEqual(len(self.conn.operations), 1)
        self.assertFalse(
            any(
                "UPDATE" in sql or "wallet_transactions" in sql and sql.startswith("INSERT")
                for sql, _ in self.conn.statements
            )
        )

    def test_ambiguous_or_incomplete_historical_facts_fail_closed(self):
        valid = ("legacy-a", "赠送", "legacy-request", 100, "order-a", 0, "CUSTOMER_STANDARD", 100)
        for rows in ([valid, valid], [(*valid[:7], None)], [(*valid[:3], None, *valid[4:])]):
            self.conn.legacy = rows
            with self.assertRaises(HTTPException) as caught:
                self.routes._replay_adjustment_operation(self.conn, **self.args())
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.conn.operations, {})


class AdjustmentBusinessTests(unittest.TestCase):
    def test_cross_operator_new_request_and_operation_id_move_balance_once(self):
        from app import admin_customer_routes as routes

        class Conn(OperationMemoryConnection):
            def __init__(self):
                super().__init__()
                self.balance = 0
                self.charges = 0

            def execute(self, sql, params=()):
                if sql.startswith("SELECT 1 FROM users") or sql.startswith("SELECT 1 FROM wallets"):
                    return Cursor((1,))
                if sql.lstrip().startswith("INSERT INTO recharge_orders"):
                    return Cursor()
                if sql.lstrip().startswith("INSERT INTO wallet_transactions"):
                    self.charges += 1
                    return Cursor()
                if sql.startswith("UPDATE wallets SET available_credits"):
                    self.balance += params[0]
                    return Cursor((self.balance,))
                if sql.lstrip().startswith("INSERT INTO admin_adjustments"):
                    return Cursor()
                return super().execute(sql, params)

        conn = Conn()
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
                    conn, "new-request"
                ),
            ),
            patch.object(routes, "_current_billing_snapshot", return_value=prices),
            patch.object(routes, "_infer_pricing_scope", return_value="CUSTOMER_STANDARD"),
            patch.object(routes, "_transaction_now_iso", return_value="2026-10-12T00:00:00Z"),
        ):
            body = routes.AdjustmentRequest(
                credits=100,
                source_document_type="FREE_GRANT",
                source_document_ref="one-source",
                reason="隔离测试",
                confirm=True,
                operation_id="first-id",
            )
            first = routes.create_admin_adjustment(
                "customer", body, None, None, SimpleNamespace(user_id="operator-a")
            )
            second = routes.create_admin_adjustment(
                "customer",
                body.model_copy(update={"operation_id": "second-id"}),
                None,
                None,
                SimpleNamespace(user_id="operator-b"),
            )
            self.assertEqual(second, first)
            self.assertEqual(conn.balance, 100)
            self.assertEqual(conn.charges, 1)
            with self.assertRaises(HTTPException) as caught:
                routes.create_admin_adjustment(
                    "customer",
                    body.model_copy(update={"credits": 200}),
                    None,
                    None,
                    SimpleNamespace(user_id="operator-b"),
                )
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(conn.balance, 100)


class RefundExecutionBusinessTests(unittest.TestCase):
    def test_valid_negative_fact_registers_once_and_second_distinct_request_conflicts(self):
        from app import admin_refund_routes as routes

        class Conn:
            registered = False
            audit_count = 0
            locked = False

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id, target_user_id"):
                    self.locked = "FOR UPDATE" in sql
                    return Cursor(("adjustment", "customer", "REFUND_APPROVAL"))
                if "FROM admin_adjustments aa" in sql:
                    assert "tx.available_delta < 0" in sql
                    return Cursor((1,))
                if sql.startswith("SELECT 1 FROM refund_executions"):
                    return Cursor((1,) if self.registered else None)
                if sql.lstrip().startswith("INSERT INTO refund_executions"):
                    assert self.locked
                    self.registered = True
                    return Cursor()
                if sql.startswith("INSERT INTO audit_logs"):
                    self.audit_count += 1
                    return Cursor()
                raise AssertionError(sql)

        conn = Conn()
        body = routes.RefundExecutionRequest(
            channel="other", provider_ref="synthetic-ref", reason="隔离测试", confirm=True
        )
        with patch.object(
            routes,
            "_write_with_idempotency",
            side_effect=lambda request, response, actor, body, business, **_: business(
                conn, "request"
            ),
        ):
            result = routes.mark_refund_execution(
                "adjustment", body, None, None, SimpleNamespace(user_id="operator")
            )
            self.assertTrue(result["executed"])
            with self.assertRaises(HTTPException) as caught:
                routes.mark_refund_execution(
                    "adjustment", body, None, None, SimpleNamespace(user_id="operator-two")
                )
            self.assertEqual(caught.exception.status_code, 409)
            self.assertEqual(conn.audit_count, 1)


def test_legacy_adjustment_transport_fingerprint_is_preserved():
    from pydantic import create_model

    from app.admin_customer_routes import AdjustmentRequest
    from app.admin_write_contract import request_hash

    old_shape = create_model(
        "OldAdjustmentRequest",
        confirm=(bool, False),
        reason=(str, ""),
        credits=(int, 0),
        source_document_type=(str, ""),
        source_document_ref=(str, ""),
    )
    payload = {
        "confirm": True,
        "reason": "隔离测试",
        "credits": 100,
        "source_document_type": "FREE_GRANT",
        "source_document_ref": "source",
    }
    route = "POST /api/control/customers/{user_id}/adjustments"
    assert request_hash(
        route, {"user_id": "customer"}, AdjustmentRequest(**payload)
    ) == request_hash(route, {"user_id": "customer"}, old_shape(**payload))


class AdjustmentMigrationRollbackTests(unittest.TestCase):
    def test_downgrade_locks_before_inspecting_or_dropping_identity_history(self):
        import importlib.util

        path = APP.parent / "migrations/versions/20261012T0100_admin_adjustment_operations.py"
        spec = importlib.util.spec_from_file_location("adjustment_operations_migration", path)
        self.assertIsNotNone(spec)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        for populated in (False, True):
            with self.subTest(populated=populated):
                statements = []

                def query(statement):
                    statements.append(str(statement))
                    return SimpleNamespace(scalar=lambda: populated)

                migration.op = SimpleNamespace(
                    get_bind=lambda: SimpleNamespace(
                        dialect=SimpleNamespace(name="postgresql"), execute=query
                    ),
                    execute=statements.append,
                )
                if populated:
                    with self.assertRaises(RuntimeError):
                        migration.downgrade()
                    self.assertFalse(any(sql.startswith("DROP") for sql in statements))
                else:
                    migration.downgrade()
                    self.assertTrue(statements[-1].startswith("DROP"))
                self.assertTrue(statements[0].startswith("LOCK TABLE"))
                self.assertIn("ACCESS EXCLUSIVE", statements[0])
