"""报价合同边界；数据库并发验收另走 PG 夹具。"""

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app import quote_contracts


@pytest.fixture
def pricing(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "service": "video_768p",
        "version": 3,
        "unit": "second",
        "units": "8.000000",
        "enabled": True,
        "unit_credits": "2",
        "unit_rounding": "ceil",
        "credits": 8,
        "discount_basis_points": 5000,
        "discount_rate": "0.5000",
        "discount_source": "manual",
        "consumption_rounding": "ceil",
        "points_per_yuan": 100,
        "free_reason": None,
    }
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(quote_contracts, "resolve_wallet_owner", lambda conn, user: "owner")
    monkeypatch.setattr(
        quote_contracts, "retail_snapshot", lambda *args, **kwargs: deepcopy(snapshot)
    )
    monkeypatch.setattr(quote_contracts, "_now", lambda conn: 1000)
    return snapshot


def issue() -> dict[str, Any]:
    return quote_contracts.issue_quote(
        None,
        user_id="child",
        service="video_768p",
        units=8,
        quantity=2,
    )


def admit(quote: dict[str, Any], **changes: Any) -> dict[str, Any]:
    request = {
        "user_id": "child",
        "service": "video_768p",
        "units": 8,
        "quantity": 2,
        "quote_token": quote["quote_token"],
        "max_credits": quote["max_credits"],
    }
    request.update(changes)
    return quote_contracts.validate_quote(None, **request)


def test_quote_binds_wallet_discount_and_exact_per_task_rounding(pricing: dict[str, Any]) -> None:
    pricing.update(unit_credits="0.15", discount_basis_points=5000, credits=1)
    quote = issue()
    assert quote["max_credits"] == 2
    accepted = admit(quote)
    assert accepted["discount_basis_points"] == 5000
    assert accepted["quote_wallet_owner_id"] == "owner"
    assert accepted["quote_max_credits"] == 2


@pytest.mark.parametrize(
    "change",
    [
        {"unit_credits": "3", "credits": 12, "version": 4},
        {"unit_credits": "1", "credits": 4, "version": 4},
        {"enabled": False, "credits": 0, "version": 4},
        {
            "discount_basis_points": 10000,
            "discount_rate": None,
            "discount_source": None,
            "credits": 16,
        },
    ],
)
def test_pricing_change_requires_new_confirmation(
    pricing: dict[str, Any], change: dict[str, Any]
) -> None:
    quote = issue()
    pricing.update(change)
    with pytest.raises(HTTPException) as error:
        admit(quote)
    assert error.value.detail["code"] == "QUOTE_CHANGED"


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "other"},
        {"service": "video_2k"},
        {"units": 9},
        {"quantity": 3},
        {"max_credits": 17},
        {"max_credits": 15},
        {"max_credits": None},
    ],
)
def test_contract_cannot_change_actor_spec_quantity_or_cap(
    pricing: dict[str, Any], changes: dict[str, Any]
) -> None:
    with pytest.raises(HTTPException):
        admit(issue(), **changes)


def test_wallet_reassignment_invalidates_quote(
    pricing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    quote = issue()
    monkeypatch.setattr(quote_contracts, "resolve_wallet_owner", lambda conn, user: "another-owner")
    with pytest.raises(HTTPException):
        admit(quote)


def test_expired_and_tampered_contracts_fail(
    pricing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    quote = issue()
    with pytest.raises(HTTPException):
        admit(quote, quote_token=quote["quote_token"][:-5] + "abcde")
    monkeypatch.setattr(quote_contracts, "_now", lambda conn: quote["quote_expires_at"])
    with pytest.raises(HTTPException) as error:
        admit(quote)
    assert error.value.detail["code"] == "QUOTE_EXPIRED"


def test_oral_quote_binds_audio_or_script(pricing: dict[str, Any]) -> None:
    pricing.update(service="oral", units="8.000000")
    quote = quote_contracts.issue_quote(
        None,
        user_id="child",
        service="oral",
        units=Decimal(8),
        specification={"script": "first"},
    )
    with pytest.raises(HTTPException):
        quote_contracts.validate_quote(
            None,
            user_id="child",
            service="oral",
            units=8,
            specification={"script": "other"},
            quote_token=quote["quote_token"],
            max_credits=quote["max_credits"],
        )


@pytest.mark.parametrize("service", ["video_768p", "video_2k", "oral"])
@pytest.mark.parametrize("max_credits", [None, 0, 16])
def test_new_paid_admission_requires_quote_even_for_legacy_clients(
    pricing: dict[str, Any], service: str, max_credits: int | None
) -> None:
    pricing.update(service=service)
    with pytest.raises(HTTPException) as error:
        quote_contracts.validate_quote(
            None,
            user_id="child",
            service=service,
            units=8,
            quantity=2,
            max_credits=max_credits,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "QUOTE_REQUIRED"
    assert "升级" in error.value.detail["message"]
    assert "确认报价" in error.value.detail["message"]


@pytest.mark.parametrize("service", ["video_768p", "video_2k", "oral"])
@pytest.mark.parametrize("free_reason", ["unconfigured", "disabled", "zero_price"])
def test_legacy_free_admission_stays_available_without_quote(
    pricing: dict[str, Any], service: str, free_reason: str
) -> None:
    pricing.update(service=service, enabled=False, credits=0, free_reason=free_reason)
    snapshot = quote_contracts.validate_quote(
        None, user_id="child", service=service, units=8, quantity=2
    )
    assert snapshot == pricing
    assert "quote_max_credits" not in snapshot


@pytest.mark.parametrize("service", ["video_768p", "video_2k", "oral"])
def test_valid_contract_still_admits_new_paid_request(
    pricing: dict[str, Any], service: str
) -> None:
    pricing.update(service=service)
    quote = quote_contracts.issue_quote(None, user_id="child", service=service, units=8, quantity=2)
    accepted = admit(quote, service=service)
    assert accepted["credits"] == 8
    assert accepted["quote_max_credits"] == 16


def test_free_admission_still_validates_a_supplied_contract(pricing: dict[str, Any]) -> None:
    pricing.update(enabled=False, credits=0)
    with pytest.raises(HTTPException) as error:
        admit(issue(), quote_token="invalid-token")
    assert error.value.detail["code"] == "QUOTE_INVALID"


def test_free_quote_cannot_turn_into_paid_admission(pricing: dict[str, Any]) -> None:
    pricing.update(enabled=False, credits=0)
    quote = issue()
    pricing.update(enabled=True, credits=8)
    with pytest.raises(HTTPException) as error:
        admit(quote)
    assert error.value.detail["code"] == "QUOTE_CHANGED"


def test_master_and_child_share_the_wallet_price(pricing: dict[str, Any]) -> None:
    master = quote_contracts.issue_quote(
        None,
        user_id="owner",
        service="video_768p",
        units=8,
        quantity=2,
    )
    assert master["max_credits"] == issue()["max_credits"]
    assert master["pricing_snapshot"] == issue()["pricing_snapshot"]


@pytest.mark.parametrize("kind", ["replica", "independent"])
def test_quote_refresh_preserves_request_identity(kind: str) -> None:
    from app.generation import GenerationBatchRequest, idempotency_request_hash
    from app.independent import IndependentVideoRequest, _independent_request_hash

    fields: dict[str, Any] = {
        "prompt_text": "有效提示词",
        "quantity": 1,
        "output_duration_seconds": 8,
        "idempotency_key": "same-attempt",
        "provider": "fake_h3",
    }
    if kind == "replica":
        request = GenerationBatchRequest(**fields, first_frame_asset_id="frame")
        fingerprint = idempotency_request_hash
    else:
        request = IndependentVideoRequest(**fields, mode="t2v", ratio="16:9")
        fingerprint = _independent_request_hash
    refreshed = request.model_copy(update={"quote_token": "fresh-token", "max_credits": 8})
    assert fingerprint(request) == fingerprint(refreshed)


@pytest.mark.parametrize("kind", ["replica", "independent"])
@pytest.mark.parametrize("quote_fields", [{}, {"quote_token": "expired-token", "max_credits": 8}])
def test_accepted_replay_ignores_missing_or_expired_quote(
    kind: str, quote_fields: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import generation, independent

    fields: dict[str, Any] = {
        "prompt_text": "有效提示词",
        "quantity": 1,
        "output_duration_seconds": 8,
        "idempotency_key": "same-attempt",
        "provider": "fake_h3",
        **quote_fields,
    }
    module = generation if kind == "replica" else independent
    monkeypatch.setattr(
        quote_contracts, "validate_quote", lambda *a, **k: pytest.fail("回放不应重新校验报价")
    )
    monkeypatch.setattr(
        module, "_reserve_generation_credit", lambda *a, **k: pytest.fail("回放不应重复预扣")
    )
    monkeypatch.setattr(module, "require_not_auditor", lambda *a, **k: None)
    monkeypatch.setattr(generation, "require_project_access", lambda *a, **k: None)
    monkeypatch.setattr(independent, "is_customer_production", lambda: False)
    monkeypatch.setattr(module, "get_generation_batch", lambda *a, **k: "original-batch")
    if kind == "replica":
        request = generation.GenerationBatchRequest(**fields, first_frame_asset_id="frame")
        monkeypatch.setattr(
            generation,
            "_find_idempotent_batch",
            lambda *a, **k: {
                "id": "original-batch",
                "request_hash": generation.idempotency_request_hash(request),
            },
        )
        result = generation.create_generation_batch(
            None,
            project_id="project",
            actor=SimpleNamespace(id="child"),
            request=request,
        )
    else:
        request = independent.IndependentVideoRequest(**fields, mode="t2v", ratio="16:9")
        monkeypatch.setattr(
            independent,
            "_find_independent_batch",
            lambda *a, **k: {
                "id": "original-batch",
                "request_hash": independent._independent_request_hash(request),
            },
        )
        result = independent.create_independent_batch(
            None,
            actor=SimpleNamespace(id="child"),
            request=request,
        )
    assert result == "original-batch"


@pytest.mark.parametrize("quote_fields", [{}, {"quote_token": "expired-token", "max_credits": 8}])
def test_accepted_oral_replay_needs_no_new_quote_or_reservation(
    quote_fields: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import oral

    fields: dict[str, Any] = {
        "identity_id": "identity",
        "avatar_id": "avatar",
        "voice_id": None,
        "mode": "AUDIO",
        "title": "已受理口播",
        "script_text": None,
        "audio_asset_id": "audio",
        "subtitle": None,
    }
    original = {
        "id": "original-task",
        "request_hash": oral._request_hash(fields),
        "status": "QUEUED",
        "submission_state": "LOCAL_PENDING",
        "estimated_cost_fen": 8,
    }
    monkeypatch.setattr(oral, "require_not_auditor", lambda *a, **k: None)
    monkeypatch.setattr(oral, "_require_own_identity", lambda *a, **k: None)
    monkeypatch.setattr(
        quote_contracts, "validate_quote", lambda *a, **k: pytest.fail("回放不应重新校验报价")
    )
    monkeypatch.setattr(
        oral, "reserve_oral_billing", lambda *a, **k: pytest.fail("回放不应重复预扣")
    )
    conn = AdmissionConnection({"oral_tasks": original})
    result = oral.create_oral_task(
        conn,
        actor=SimpleNamespace(id="child"),
        idempotency_key="same-attempt",
        **fields,
        **quote_fields,
    )
    assert result.task_id == "original-task"
    assert result.replayed is True
    assert result.estimated_cost_fen == 8
    assert len(conn.statements) == 1


class AdmissionConnection:
    """隔离受理入口的只读替身；任何任务落库或钱包写入都会立即失败。"""

    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = rows or {}
        self.statements: list[str] = []
        self.rollbacks = 0

    def execute(self, sql: str, params: Any = None) -> Any:
        statement = " ".join(sql.split())
        self.statements.append(statement)
        if statement == "BEGIN IMMEDIATE":
            return SimpleNamespace()
        for table, row in self.rows.items():
            if statement.startswith("SELECT ") and f"FROM {table} " in statement:
                return SimpleNamespace(fetchone=lambda: row)
        pytest.fail(f"报价拒绝或幂等回放前不应执行其他操作：{statement}")

    def rollback(self) -> None:
        self.rollbacks += 1

    def __enter__(self) -> "AdmissionConnection":
        return self

    def __exit__(self, *args: Any) -> None:
        self.rollback()


@pytest.mark.parametrize("kind", ["replica", "independent", "oral"])
def test_new_paid_entrypoints_reject_legacy_request_before_task_or_wallet_write(
    kind: str, pricing: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import billing_catalog, generation, independent, oral

    actor = SimpleNamespace(id="child", role="customer")
    module = {"replica": generation, "independent": independent, "oral": oral}[kind]
    monkeypatch.setattr(module, "require_not_auditor", lambda *a, **k: None)
    conn = AdmissionConnection()
    if kind == "oral":
        pricing.update(service="oral")
        conn.rows = {
            "oral_tasks": None,
            "oral_avatars": {
                "id": "avatar",
                "status": "READY",
                "identity_id": "identity",
                "source_kind": "VIDEO",
                "source_asset_id": "source",
                "original_source_asset_id": None,
                "consent_id": "consent",
            },
        }
        monkeypatch.setattr(oral, "_require_own_identity", lambda *a, **k: None)
        monkeypatch.setattr(oral, "_require_valid_consent", lambda *a, **k: None)
        monkeypatch.setattr(
            oral, "_require_biometric_source_asset", lambda *a, **k: {"sha256": "s"}
        )
        monkeypatch.setattr(oral, "_require_source_asset", lambda *a, **k: {})
        monkeypatch.setattr(oral, "_require_audio_purpose", lambda *a, **k: None)
        monkeypatch.setattr(billing_catalog, "oral_budget_units", lambda *a, **k: Decimal(8))

        def create() -> Any:
            return oral.create_oral_task(
                conn,
                actor=actor,
                identity_id="identity",
                avatar_id="avatar",
                voice_id=None,
                mode="AUDIO",
                title="新口播",
                script_text=None,
                audio_asset_id="audio",
                subtitle=None,
                idempotency_key="new-attempt",
            )
    else:
        monkeypatch.setattr(module, "is_customer_production", lambda: False)
        monkeypatch.setattr(
            module, "read_runtime_limits", lambda *a: {"max_generation_count_per_batch": 4}
        )
        fields: dict[str, Any] = {
            "quantity": 1,
            "output_duration_seconds": 8,
            "idempotency_key": "new-attempt",
            "provider": "fake_h3",
        }
        if kind == "replica":
            monkeypatch.setattr(generation, "require_project_access", lambda *a, **k: None)
            monkeypatch.setattr(generation, "_find_idempotent_batch", lambda *a, **k: None)
            monkeypatch.setattr(generation, "version_stale_reasons", lambda *a, **k: [])
            monkeypatch.setattr(generation, "require_confirmed_first_frame", lambda *a, **k: None)
            monkeypatch.setattr(
                generation,
                "require_asset_access",
                lambda *a, **k: {"project_id": "project", "storage_uri": "local://frame"},
            )
            monkeypatch.setattr(
                generation,
                "require_version",
                lambda *a, **k: {
                    "id": "prompt",
                    "payload_json": json.dumps(
                        {"status": "LOCKED", "first_frame_asset_id": "frame", "content_hash": "h"}
                    ),
                },
            )
            replica_request = generation.GenerationBatchRequest(
                **fields, prompt_version_id="prompt", first_frame_asset_id="frame"
            )

            def create() -> Any:
                return generation.create_generation_batch(
                    conn, actor=actor, project_id="project", request=replica_request
                )
        else:
            monkeypatch.setattr(independent, "_find_independent_batch", lambda *a, **k: None)
            independent_request = independent.IndependentVideoRequest(
                **fields, prompt_text="有效提示词", mode="t2v", ratio="16:9"
            )

            def create() -> Any:
                return independent.create_independent_batch(
                    conn, actor=actor, request=independent_request
                )

    with pytest.raises(HTTPException) as error:
        create()
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "QUOTE_REQUIRED"
    assert conn.rollbacks == 1


class BillingConnection:
    """只检查真实计费函数的 SQL 调用及快照参数，不替代 PG 事务并发验收。"""

    api_key_id = None
    auth_source = "test"

    def __init__(self) -> None:
        self.operation: dict[str, Any] | None = None
        self.wallet_updates: list[Any] = []
        self.inserts = 0

    def execute(self, sql: str, params: Any = None) -> Any:
        import json
        from types import SimpleNamespace

        row: Any = None
        if sql.startswith("SELECT id, budget_units") or sql.startswith(
            "SELECT * FROM billing_operations"
        ):
            row = self.operation
        elif sql.startswith("SELECT available_credits"):
            row = (100,)
        elif sql.startswith("UPDATE wallets"):
            self.wallet_updates.append(params)
        elif sql.startswith("INSERT INTO billing_operations"):
            self.inserts += 1
            self.operation = {
                "id": params[0],
                "user_id": params[1],
                "budget_units": params[11],
                "reserved_credits": params[12],
                "request_fingerprint": params[14],
                "pricing_snapshot_json": params[9],
                "funding_json": params[13],
                "billing_round": params[5],
                "state": "PENDING",
            }
            assert json.loads(params[9])["credits"] == params[12]
        return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: [], rowcount=1)


@pytest.fixture
def billing(monkeypatch: pytest.MonkeyPatch) -> BillingConnection:
    from app import usage_billing

    monkeypatch.setattr(usage_billing, "resolve_wallet_owner", lambda *a: "owner")
    monkeypatch.setattr(usage_billing, "enforce_sub_account_feature", lambda *a, **k: None)
    monkeypatch.setattr(usage_billing, "enforce_sub_account_quota", lambda *a, **k: None)
    return BillingConnection()


def test_reservation_limit_checked_before_wallet_mutation(
    pricing: dict[str, Any],
    billing: BillingConnection,
) -> None:
    from app.usage_billing import accept_operation

    snapshot = admit(issue())
    with pytest.raises(HTTPException) as error:
        accept_operation(
            billing,
            user_id="child",
            service="video_768p",
            source_id="task",
            units=9,
            pricing_snapshot=snapshot,
        )
    assert error.value.detail["code"] == "QUOTE_LIMIT_EXCEEDED"
    assert billing.wallet_updates == []
    assert billing.inserts == 0


def test_admitted_snapshot_survives_price_change_and_repeated_operation(
    pricing: dict[str, Any],
    billing: BillingConnection,
) -> None:
    import json

    from app.usage_billing import accept_operation, finish_operation

    snapshot = admit(issue())
    operation = accept_operation(
        billing,
        user_id="child",
        service="video_768p",
        source_id="task",
        units=8,
        pricing_snapshot=snapshot,
    )
    pricing.update(unit_credits="100", credits=800, version=4, discount_basis_points=10000)
    repeated = accept_operation(
        billing,
        user_id="child",
        service="video_768p",
        source_id="task",
        units=8,
    )
    assert repeated == operation
    assert billing.inserts == 1
    assert billing.wallet_updates == [(8, 8, "owner", 8)]
    assert billing.operation is not None
    frozen = json.loads(billing.operation["pricing_snapshot_json"])
    assert frozen["version"] == 3
    assert frozen["discount_basis_points"] == 5000
    assert finish_operation(billing, operation_id=operation, units=20, succeeded=True) == 8


class RegenerationConnection:
    """原任务只读快照与写入记录，不建立任何数据库连接。"""

    def __init__(self) -> None:
        self.batch: dict[str, Any] = {
            "id": "source-batch",
            "project_id": "project",
            "created_by_user_id": "child",
            "creation_kind": "replica",
            "request_snapshot_json": json.dumps(
                {"quantity": 1, "resolution": "768P", "output_duration_seconds": 8}
            ),
        }
        self.tasks = [
            {
                **self.batch,
                "id": "source-task",
                "batch_id": "source-batch",
                "generation_mode": "I2V",
                "provider": "fake_h3",
                "model": "test-model",
                "prompt_version_id": "prompt",
                "prompt_snapshot_json": json.dumps(
                    {"resolution": "768P", "output_duration_seconds": 8}
                ),
                "superseded_by_task_id": None,
            }
        ]
        self.writes: list[str] = []
        self.admission_locks: list[str] = []
        self.rollbacks = 0
        self.commits = 0
        # 写入返回的 rowcount 可在用例中改写为 0，模拟 CAS 命中并发冲突。
        self.write_rowcount = 1
        self.billing_operation: dict[str, Any] | None = None

    def execute(self, sql: str, params: Any = None) -> Any:
        statement = " ".join(sql.split())
        if statement.startswith("SELECT pg_advisory_xact_lock("):
            self.admission_locks.append(params[0])
            return SimpleNamespace()
        if statement == "BEGIN IMMEDIATE":
            return SimpleNamespace()
        if statement.startswith("SELECT "):
            if "FROM billing_operations" in statement:
                return SimpleNamespace(fetchone=lambda: self.billing_operation)
            row = self.tasks[0] if "FROM generation_tasks" in statement else self.batch
            return SimpleNamespace(fetchone=lambda: row, fetchall=lambda: self.tasks)
        self.writes.append(statement)
        return SimpleNamespace(rowcount=self.write_rowcount)

    def rollback(self) -> None:
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def regeneration(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app import generation

    reservations: list[dict[str, Any]] = []
    for name in (
        "require_not_auditor",
        "require_batch_access",
        "require_regeneration_provider_ready",
        "require_task_paid_regeneration_state",
        "snapshot_generation_rates",
        "ensure_user_queue_cursor",
        "insert_audit",
        "_refresh_batch_status_in_transaction",
    ):
        monkeypatch.setattr(generation, name, lambda *a, **k: None)
    monkeypatch.setattr(generation, "_find_idempotent_batch", lambda *a, **k: None)
    monkeypatch.setattr(
        generation, "read_runtime_limits", lambda *a: {"max_generation_count_per_batch": 4}
    )
    monkeypatch.setattr(
        generation, "_reserve_generation_credit", lambda *a, **k: reservations.append(k)
    )
    monkeypatch.setattr(generation, "get_generation_batch", lambda *a, **k: {"id": k["batch_id"]})
    return {
        "conn": RegenerationConnection(),
        "actor": SimpleNamespace(id="child", role="customer"),
        "reservations": reservations,
    }


def regenerate(kind: str, scene: dict[str, Any], request: Any) -> Any:
    from app import generation

    common = {"actor": scene["actor"], "request": request}
    if kind == "batch":
        return generation.regenerate_generation_batch(
            scene["conn"], batch_id="source-batch", **common
        )
    return generation.regenerate_generation_task(scene["conn"], task_id="source-task", **common)


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_new_paid_regeneration_rejects_missing_quote_before_reservation(
    kind: str, pricing: dict[str, Any], regeneration: dict[str, Any]
) -> None:
    from app.generation import PaidRegenerationRequest

    request = PaidRegenerationRequest(
        idempotency_key="new-regeneration", generation_reason="重新生成"
    )
    with pytest.raises(HTTPException) as error:
        regenerate(kind, regeneration, request)
    assert error.value.detail["code"] == "QUOTE_REQUIRED"
    assert regeneration["conn"].admission_locks == [
        'generation:paid-regeneration:["child","project","new-regeneration"]'
    ]
    assert regeneration["reservations"] == []
    assert regeneration["conn"].writes == []
    assert regeneration["conn"].commits == 0


@pytest.mark.parametrize("kind", ["batch", "task"])
@pytest.mark.parametrize("quote_fields", [{}, {"quote_token": "expired-token", "max_credits": 8}])
def test_paid_regeneration_replay_returns_original_without_quote_or_reservation(
    kind: str,
    quote_fields: dict[str, Any],
    regeneration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import generation

    request = generation.PaidRegenerationRequest(
        idempotency_key="accepted-regeneration", generation_reason="重新生成"
    ).model_copy(update=quote_fields)
    request_hash = generation.paid_regeneration_request_hash(
        request,
        operation_kind="MANUAL_BATCH_REGENERATE" if kind == "batch" else "TASK_PAID_REGENERATE",
        source_batch_id="source-batch",
        source_task_ids=["source-task"],
        request_snapshot=regeneration["conn"].batch["request_snapshot_json"],
        prompt_snapshots=[regeneration["conn"].tasks[0]["prompt_snapshot_json"]],
    )
    monkeypatch.setattr(
        generation,
        "_find_idempotent_batch",
        lambda *a, **k: {"id": "accepted-batch", "request_hash": request_hash},
    )
    monkeypatch.setattr(
        quote_contracts, "validate_quote", lambda *a, **k: pytest.fail("回放不应重新校验报价")
    )
    result = regenerate(kind, regeneration, request)
    assert result == {"id": "accepted-batch"}
    assert regeneration["reservations"] == []
    assert regeneration["conn"].writes == []


def regeneration_quote(kind: str, scene: dict[str, Any]) -> Any:
    from app import generation_routes

    if kind == "batch":
        return generation_routes.read_batch_regeneration_quote(
            "source-batch", scene["conn"], scene["actor"]
        )
    return generation_routes.read_task_regeneration_quote(
        "source-task", scene["conn"], scene["actor"]
    )


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_verified_regeneration_quote_is_reserved_once_for_new_paid_round(
    kind: str, pricing: dict[str, Any], regeneration: dict[str, Any]
) -> None:
    from app.generation import PaidRegenerationRequest

    if kind == "batch":
        regeneration["conn"].tasks.append({**regeneration["conn"].tasks[0], "id": "second-task"})
    quote = regeneration_quote(kind, regeneration)
    assert quote.duration_seconds == 8
    assert quote.quantity == (2 if kind == "batch" else 1)
    assert quote.max_credits == quote.estimated_credits == 8 * quote.quantity
    assert regeneration["conn"].writes == []
    request = PaidRegenerationRequest(
        idempotency_key="new-regeneration",
        generation_reason="重新生成",
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
    )
    result = regenerate(kind, regeneration, request)
    assert result["id"] != "source-batch"
    assert len(regeneration["reservations"]) == quote.quantity
    assert len({r["task_id"] for r in regeneration["reservations"]}) == quote.quantity
    assert regeneration["conn"].commits == 1
    for reservation in regeneration["reservations"]:
        assert reservation["user_id"] == "child"
        assert reservation["seconds"] == 8
        assert reservation["pricing_snapshot"]["quote_max_credits"] == quote.max_credits
        assert reservation["pricing_snapshot"]["quote_wallet_owner_id"] == "owner"
        assert reservation["pricing_snapshot"]["quote_quantity"] == quote.quantity


@pytest.mark.parametrize("kind", ["batch", "task"])
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("tampered", "QUOTE_INVALID"),
        ("expired", "QUOTE_EXPIRED"),
        ("price", "QUOTE_CHANGED"),
        ("snapshot", "QUOTE_MISMATCH"),
        ("actor", "QUOTE_MISMATCH"),
        ("cap", "QUOTE_LIMIT_EXCEEDED"),
        ("missing_cap", "QUOTE_LIMIT_EXCEEDED"),
        ("wrong_operation", "QUOTE_MISMATCH"),
        ("generic_video_quote", "QUOTE_MISMATCH"),
    ],
)
def test_invalid_regeneration_quote_rejects_before_any_new_charge(
    kind: str,
    change: str,
    code: str,
    pricing: dict[str, Any],
    regeneration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation import PaidRegenerationRequest, generation_price_quote

    quote_kind = ("batch" if kind == "task" else "task") if change == "wrong_operation" else kind
    quote = regeneration_quote(quote_kind, regeneration)
    if change == "generic_video_quote":
        quote = generation_price_quote(
            None, user_id="child", resolution="768P", duration_seconds=8, quantity=1
        )
    fields = {"quote_token": quote.quote_token, "max_credits": quote.max_credits}
    if change == "tampered":
        fields["quote_token"] = "invalid-token"
    elif change == "expired":
        monkeypatch.setattr(quote_contracts, "_now", lambda conn: quote.quote_expires_at)
    elif change == "price":
        pricing.update(version=4, credits=12)
    elif change == "snapshot":
        regeneration["conn"].tasks[0]["prompt_snapshot_json"] = json.dumps(
            {"resolution": "768P", "output_duration_seconds": 8, "prompt_text": "changed"}
        )
    elif change == "actor":
        regeneration["actor"] = SimpleNamespace(id="admin", role="admin")
    elif change == "cap":
        fields["max_credits"] = 0
    elif change == "missing_cap":
        fields.pop("max_credits")
    request = PaidRegenerationRequest(
        idempotency_key="new-regeneration", generation_reason="重新生成", **fields
    )
    with pytest.raises(HTTPException) as error:
        regenerate(kind, regeneration, request)
    assert error.value.detail["code"] == code
    assert regeneration["reservations"] == []
    assert regeneration["conn"].writes == []
    assert regeneration["conn"].commits == 0


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_regeneration_quote_uses_original_paying_wallet_when_admin_operates(
    kind: str,
    pricing: dict[str, Any],
    regeneration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.generation import PaidRegenerationRequest

    users: list[str] = []
    regeneration["actor"] = SimpleNamespace(id="admin", role="admin")

    def owner(conn: Any, user_id: str) -> str:
        users.append(user_id)
        return "owner"

    monkeypatch.setattr(quote_contracts, "resolve_wallet_owner", owner)
    quote = regeneration_quote(kind, regeneration)
    request = PaidRegenerationRequest(
        idempotency_key="new-regeneration",
        generation_reason="重新生成",
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
    )
    regenerate(kind, regeneration, request)
    assert users == ["child", "child"]
    assert regeneration["reservations"][0]["pricing_snapshot"]["quote_actor_user_id"] == "child"


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_free_regeneration_preserves_legacy_no_quote_path(
    kind: str, pricing: dict[str, Any], regeneration: dict[str, Any]
) -> None:
    from app.generation import PaidRegenerationRequest

    pricing.update(enabled=False, credits=0, free_reason="disabled")
    request = PaidRegenerationRequest(
        idempotency_key="free-regeneration", generation_reason="重新生成"
    )
    regenerate(kind, regeneration, request)
    assert regeneration["reservations"][0]["pricing_snapshot"]["credits"] == 0
    assert regeneration["conn"].commits == 1


def test_mixed_historical_batch_requires_per_task_quote(
    pricing: dict[str, Any], regeneration: dict[str, Any]
) -> None:
    regeneration["conn"].tasks.append(
        {
            **regeneration["conn"].tasks[0],
            "id": "second-task",
            "prompt_snapshot_json": json.dumps({"resolution": "2K", "output_duration_seconds": 9}),
        }
    )
    with pytest.raises(HTTPException) as error:
        regeneration_quote("batch", regeneration)
    assert error.value.detail["code"] == "QUOTE_SPECIFICATION_MISMATCH"
    assert regeneration["conn"].writes == []


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_regeneration_quote_checks_source_access_before_pricing(
    kind: str, regeneration: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import generation

    def deny(*args: Any, **kwargs: Any) -> None:
        raise HTTPException(403, detail={"code": "SOURCE_FORBIDDEN"})

    monkeypatch.setattr(generation, "require_batch_access", deny)
    monkeypatch.setattr(
        quote_contracts, "issue_quote", lambda *a, **k: pytest.fail("无权限的原任务不得报价")
    )
    with pytest.raises(HTTPException) as error:
        regeneration_quote(kind, regeneration)
    assert error.value.status_code == 403
    assert regeneration["conn"].writes == []


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_regeneration_contract_cannot_be_reused_for_another_source(
    kind: str, pricing: dict[str, Any], regeneration: dict[str, Any]
) -> None:
    from app import generation

    quote = regeneration_quote(kind, regeneration)
    request = generation.PaidRegenerationRequest(
        idempotency_key="other-source",
        generation_reason="重新生成",
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
    )
    with pytest.raises(HTTPException) as error:
        if kind == "batch":
            generation.regenerate_generation_batch(
                regeneration["conn"],
                actor=regeneration["actor"],
                batch_id="other-batch",
                request=request,
            )
        else:
            generation.regenerate_generation_task(
                regeneration["conn"],
                actor=regeneration["actor"],
                task_id="other-task",
                request=request,
            )
    assert error.value.detail["code"] == "QUOTE_MISMATCH"
    assert regeneration["reservations"] == []
    assert regeneration["conn"].writes == []


def test_regeneration_quote_refresh_preserves_hash_but_business_changes_do_not() -> None:
    from app import generation

    request = generation.PaidRegenerationRequest(
        idempotency_key="same-key", generation_reason="重新生成", estimated_cost_snapshot=1
    )
    fields: dict[str, Any] = {
        "operation_kind": "TASK_PAID_REGENERATE",
        "source_batch_id": "batch",
        "source_task_ids": ["task"],
        "request_snapshot": "{}",
        "prompt_snapshots": ['{"resolution":"768P","output_duration_seconds":8}'],
    }
    fingerprint = generation.paid_regeneration_request_hash(request, **fields)
    refreshed = request.model_copy(update={"quote_token": "fresh-token", "max_credits": 8})
    assert generation.paid_regeneration_request_hash(refreshed, **fields) == fingerprint
    changed = request.model_copy(update={"generation_reason": "不同原因"})
    assert generation.paid_regeneration_request_hash(changed, **fields) != fingerprint
    fields["source_task_ids"] = ["other-task"]
    assert generation.paid_regeneration_request_hash(request, **fields) != fingerprint


@pytest.mark.parametrize("kind", ["batch", "task"])
def test_regeneration_replay_still_rejects_changed_business_request(
    kind: str, regeneration: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import generation

    monkeypatch.setattr(
        generation,
        "_find_idempotent_batch",
        lambda *a, **k: {"id": "original-batch", "request_hash": "different-business-request"},
    )
    request = generation.PaidRegenerationRequest(
        idempotency_key="same-key", generation_reason="不同原因"
    )
    with pytest.raises(HTTPException) as error:
        regenerate(kind, regeneration, request)
    assert error.value.detail["code"] == "IDEMPOTENCY_CONFLICT"
    assert regeneration["reservations"] == []
    assert regeneration["conn"].writes == []


def test_regeneration_quote_routes_and_optional_replay_fields_are_exposed() -> None:
    from fastapi import FastAPI

    from app.generation_routes import router

    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()
    for resource, identifier in [
        ("generation-tasks", "task_id"),
        ("generation-batches", "batch_id"),
    ]:
        route = schema["paths"][f"/api/{resource}/{{{identifier}}}/regeneration-quote"]["get"]
        response = route["responses"]["200"]["content"]["application/json"]["schema"]
        assert response == {"$ref": "#/components/schemas/GenerationPriceQuote"}
    request_schema = schema["components"]["schemas"]["PaidRegenerationRequest"]
    assert {"quote_token", "max_credits"} <= request_schema["properties"].keys()
    assert not {"quote_token", "max_credits"}.intersection(request_schema["required"])


def test_archive_only_retry_remains_free_without_quote(
    regeneration: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import generation

    regeneration["conn"].tasks[0].update(
        status="SUCCEEDED",
        archive_status="ARCHIVE_FAILED",
        quality_status="PASSED",
        provider_task_id="existing-provider-task",
        provider_result_url="https://result.example/video.mp4",
        result_asset_id=None,
        archive_retry_count=0,
        error_code=None,
        submitted_at="2026-10-01T00:00:00Z",
    )
    monkeypatch.setattr(generation, "_idempotent_task_operation", lambda *a, **k: None)
    monkeypatch.setattr(generation, "_record_completed_task_operation", lambda *a, **k: None)
    monkeypatch.setattr(generation, "get_task_result", lambda *a: {"id": "source-task"})
    monkeypatch.setattr(
        quote_contracts, "validate_quote", lambda *a, **k: pytest.fail("免费归档重试不应校验报价")
    )
    result = generation.retry_generation_task(
        regeneration["conn"],
        actor=regeneration["actor"],
        task_id="source-task",
        request=generation.GenerationTaskRetryRequest(
            idempotency_key="archive-retry", retry_reason="重新归档"
        ),
    )
    assert result == {"id": "source-task"}
    assert regeneration["reservations"] == []
    assert regeneration["conn"].commits == 1


@pytest.fixture
def pre_provider_retry(
    pricing: dict[str, Any], regeneration: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    from app import generation

    regeneration["conn"].tasks[0].update(
        status="FAILED",
        archive_status="PENDING",
        quality_status="PENDING",
        provider_task_id=None,
        provider_result_url=None,
        result_asset_id=None,
        archive_retry_count=0,
        error_code=sorted(generation.SAFE_PRE_PROVIDER_FAILURE_CODES)[0],
        submitted_at=None,
    )
    accepted = admit(issue())
    regeneration["conn"].billing_operation = {
        "user_id": "child",
        "service": "video_768p",
        "state": "FAILED",
        "budget_units": "8.000000",
        "reserved_credits": 8,
        "charged_credits": 0,
        "pricing_snapshot_json": json.dumps(accepted),
    }
    monkeypatch.setattr(generation, "_idempotent_task_operation", lambda *a, **k: None)
    monkeypatch.setattr(generation, "_record_completed_task_operation", lambda *a, **k: None)
    monkeypatch.setattr(generation, "get_task_result", lambda *a: {"id": "source-task"})
    return regeneration


def retry_before_provider(scene: dict[str, Any]) -> Any:
    from app import generation

    return generation.retry_generation_task(
        scene["conn"],
        actor=scene["actor"],
        task_id="source-task",
        request=generation.GenerationTaskRetryRequest(
            idempotency_key="pre-provider-retry", retry_reason="服务恢复后继续"
        ),
    )


def test_released_pre_provider_retry_reuses_accepted_quote_despite_price_or_expiry_change(
    pricing: dict[str, Any], pre_provider_retry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pricing.update(unit_credits="100", credits=800, version=4)
    monkeypatch.setattr(quote_contracts, "_now", lambda conn: 999999)
    result = retry_before_provider(pre_provider_retry)
    assert result == {"id": "source-task"}
    reservation = pre_provider_retry["reservations"][0]
    assert reservation["billing_round"] is None
    assert reservation["pricing_snapshot"]["credits"] == 8
    assert reservation["pricing_snapshot"]["quote_max_credits"] == 16
    assert reservation["pricing_snapshot"]["version"] == 3


def test_pending_pre_provider_retry_keeps_existing_operation_without_new_quote(
    pre_provider_retry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = pre_provider_retry["conn"].billing_operation
    operation.update(state="PENDING", pricing_snapshot_json=None)
    monkeypatch.setattr(
        quote_contracts, "validate_quote", lambda *a, **k: pytest.fail("原预扣仍在时不应索要新报价")
    )
    retry_before_provider(pre_provider_retry)
    assert pre_provider_retry["reservations"][0].get("pricing_snapshot") is None


@pytest.mark.parametrize(
    "defect", ["absent", "missing", "legacy", "cap", "units", "owner", "settled"]
)
def test_released_pre_provider_retry_requires_verifiable_original_quote(
    defect: str, pre_provider_retry: dict[str, Any]
) -> None:
    operation = pre_provider_retry["conn"].billing_operation
    if defect == "absent":
        pre_provider_retry["conn"].billing_operation = None
    elif defect == "missing":
        operation["pricing_snapshot_json"] = None
    elif defect == "settled":
        operation["state"] = "SUCCEEDED"
    else:
        snapshot = json.loads(operation["pricing_snapshot_json"])
        if defect == "legacy":
            snapshot.pop("quote_max_credits")
        elif defect == "cap":
            snapshot["quote_max_credits"] = 7
        elif defect == "units":
            operation["budget_units"] = "9.000000"
        elif defect == "owner":
            snapshot["quote_actor_user_id"] = "another-user"
        operation["pricing_snapshot_json"] = json.dumps(snapshot)
    with pytest.raises(HTTPException) as error:
        retry_before_provider(pre_provider_retry)
    assert error.value.detail["code"] == "QUOTE_REQUIRED"
    assert pre_provider_retry["reservations"] == []
    assert pre_provider_retry["conn"].writes == []


def test_free_pre_provider_retry_keeps_original_zero_price_when_current_price_is_paid(
    pricing: dict[str, Any], pre_provider_retry: dict[str, Any]
) -> None:
    operation = pre_provider_retry["conn"].billing_operation
    snapshot = json.loads(operation["pricing_snapshot_json"])
    snapshot.update(enabled=False, credits=0, free_reason="disabled")
    snapshot = {key: value for key, value in snapshot.items() if not key.startswith("quote_")}
    operation.update(reserved_credits=0, pricing_snapshot_json=json.dumps(snapshot))
    pricing.update(enabled=True, unit_credits="100", credits=800, version=4)
    retry_before_provider(pre_provider_retry)
    assert pre_provider_retry["reservations"][0]["pricing_snapshot"]["credits"] == 0


@pytest.mark.parametrize(
    "request_payload,prompt_payload",
    [
        ({"resolution": "2K", "output_duration_seconds": 8}, {"prompt_text": "历史缺规格"}),
        (
            {"resolution": "2K", "output_duration_seconds": 8},
            {"resolution": "768P", "output_duration_seconds": 8},
        ),
        (
            {"resolution": "768P", "output_duration_seconds": 8},
            {"resolution": "768P", "output_duration_seconds": 6},
        ),
        ({"quantity": 1}, {"resolution": "768P", "output_duration_seconds": 8}),
    ],
)
def test_regeneration_quote_never_guesses_or_disagrees_with_worker_specs(
    request_payload,
    prompt_payload,
):
    from app.generation import _regeneration_quote_parameters

    with pytest.raises(HTTPException):
        _regeneration_quote_parameters(
            actor_id="actor",
            operation_kind="TASK_PAID_REGENERATE",
            source_batch_id="batch",
            source_task_ids=["task"],
            request_snapshot=json.dumps(request_payload),
            prompt_snapshots=[json.dumps(prompt_payload)],
        )


# ---------------------------------------------------------------------------
# R0（深审 2026-10-31 唯一 P0）：批级付费重生成的终态门禁与逐任务 CAS supersede。
# 批级入口此前绕过任务级门禁，对进行中批次会双份生成 + 双份扣费。
# 注：CAS 冲突（rowcount≠1→409）分支与任务级实现逐字同构，未建假连接用例——
# Mimosa 候选扫描对测试替身的 execute 重载误报 SQL 注入（两次拦截），
# 冲突路径由任务级既有实现背书 + 集成门禁兜底。
# ---------------------------------------------------------------------------


def _paid_regenerable_task(**overrides: Any) -> dict[str, Any]:
    """满足真实终态门禁的源任务行（SUCCEEDED + DIRECT 归档 + 已提交）。"""
    row: dict[str, Any] = {
        "id": "source-task",
        "batch_id": "source-batch",
        "generation_mode": "I2V",
        "provider": "fake_h3",
        "model": "test-model",
        "prompt_version_id": "prompt",
        "prompt_snapshot_json": json.dumps({"resolution": "768P", "output_duration_seconds": 8}),
        "status": "SUCCEEDED",
        "archive_status": "DIRECT",
        "quality_status": "NOT_REQUIRED",
        "quality_issue_codes": "[]",
        "provider_task_id": "prov-1",
        "provider_result_url": "https://example.test/video",
        "submitted_at": "2026-11-01 00:00:00",
        "superseded_by_task_id": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def batch_gate_scene(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """与 regeneration 夹具同构，但保留真实的任务级终态门禁不被打桩。"""
    from app import generation

    for name in (
        "require_not_auditor",
        "require_batch_access",
        "_refresh_batch_status_in_transaction",
    ):
        monkeypatch.setattr(generation, name, lambda *a, **k: None)
    monkeypatch.setattr(generation, "_find_idempotent_batch", lambda *a, **k: None)
    conn = RegenerationConnection()
    conn.tasks = [_paid_regenerable_task()]
    return {"conn": conn, "actor": SimpleNamespace(id="child", role="customer")}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"status": "RUNNING"}, "PAID_REGENERATION_NOT_ALLOWED"),
        ({"status": "SUBMISSION_UNCERTAIN"}, "MUST_RECONCILE_SUBMISSION"),
        ({"archive_status": "ARCHIVE_FAILED"}, "ARCHIVE_RETRY_ONLY"),
    ],
)
def test_batch_regeneration_enforces_task_level_terminal_gate(
    overrides: dict[str, Any], code: str, batch_gate_scene: dict[str, Any]
) -> None:
    from app.generation import PaidRegenerationRequest

    batch_gate_scene["conn"].tasks = [
        _paid_regenerable_task(),
        _paid_regenerable_task(id="second-task", **overrides),
    ]
    request = PaidRegenerationRequest(idempotency_key="gated-batch", generation_reason="重新生成")
    with pytest.raises(HTTPException) as error:
        regenerate("batch", batch_gate_scene, request)
    assert error.value.detail["code"] == code
    # 门禁必须在任何写入/预扣之前拦截，异常路径整单回滚。
    assert batch_gate_scene["conn"].writes == []
    assert batch_gate_scene["conn"].commits == 0


def test_batch_regeneration_rejects_superseded_source_before_any_write(
    batch_gate_scene: dict[str, Any],
) -> None:
    from app.generation import PaidRegenerationRequest

    batch_gate_scene["conn"].tasks[0]["superseded_by_task_id"] = "prior-replacement"
    request = PaidRegenerationRequest(
        idempotency_key="superseded-batch", generation_reason="重新生成"
    )
    with pytest.raises(HTTPException) as error:
        regenerate("batch", batch_gate_scene, request)
    assert error.value.detail["code"] == "SOURCE_TASK_ALREADY_SUPERSEDED"
    assert batch_gate_scene["conn"].writes == []
    assert batch_gate_scene["conn"].commits == 0


def test_batch_regeneration_supersedes_every_source_task(
    pricing: dict[str, Any],
    regeneration: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import generation
    from app.generation import PaidRegenerationRequest

    regeneration["conn"].tasks.append({**regeneration["conn"].tasks[0], "id": "second-task"})
    refresh_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        generation,
        "_refresh_batch_status_in_transaction",
        lambda *a, **k: refresh_calls.append(k),
    )
    quote = regeneration_quote("batch", regeneration)
    request = PaidRegenerationRequest(
        idempotency_key="supersede-batch",
        generation_reason="重新生成",
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
    )
    result = regenerate("batch", regeneration, request)
    assert result["id"] != "source-batch"
    # 写入构成：批次 INSERT ×1 + 任务 INSERT ×2 + 逐任务 CAS supersede ×2。
    assert len(regeneration["conn"].writes) == 5
    # 源批次与任务级路径一致地刷新状态。
    assert refresh_calls == [{"batch_id": "source-batch"}]
    assert regeneration["conn"].commits == 1
    assert len(regeneration["reservations"]) == 2


def test_batch_regeneration_cas_conflict_rolls_back_whole_order(
    pricing: dict[str, Any],
    regeneration: dict[str, Any],
) -> None:
    """CAS 写返回 rowcount=0（源任务已被并发请求替换）→ 409、不提交、回滚。"""
    from app.generation import PaidRegenerationRequest

    regeneration["conn"].write_rowcount = 0
    quote = regeneration_quote("batch", regeneration)
    request = PaidRegenerationRequest(
        idempotency_key="racing-batch",
        generation_reason="重新生成",
        quote_token=quote.quote_token,
        max_credits=quote.max_credits,
    )
    with pytest.raises(HTTPException) as error:
        regenerate("batch", regeneration, request)
    assert error.value.detail["code"] == "SOURCE_TASK_ALREADY_SUPERSEDED"
    assert regeneration["conn"].commits == 0
    assert regeneration["conn"].rollbacks >= 1
