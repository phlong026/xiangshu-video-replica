"""人物同次受理契约；数据库接口替身，不冒充 PostgreSQL 并发集成验证。"""

from __future__ import annotations

import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app import image_tasks, usage_billing
from app.auth import CurrentUser


class AdmissionStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.requests: dict[tuple[str, str], dict[str, str]] = {}
        self.lock = threading.RLock()
        self.reservations: list[dict[str, Any]] = []


class AdmissionConnection:
    def __init__(self, store: AdmissionStore) -> None:
        self.store = store
        self.held = False
        self.row: dict[str, Any] | None = None
        self.rowcount = 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> AdmissionConnection:
        sql = " ".join(sql.split())
        self.row = None
        self.rowcount = 0
        if "pg_advisory_xact_lock" in sql:
            self.store.lock.acquire()
            self.held = True
        elif sql.startswith("INSERT INTO character_sheet_task_requests"):
            self.store.requests.setdefault(
                (params[0], params[1]), {"request_hash": params[2], "task_id": params[3]}
            )
        elif "SELECT request_hash, task_id FROM character_sheet_task_requests" in sql:
            self.row = self.store.requests.get((params[0], params[1]))
        elif "SELECT t.* FROM character_sheet_task_requests" in sql:
            binding = self.store.requests.get((params[0], params[1]))
            if binding:
                self.row = next((r for r in self.store.rows if r["id"] == binding["task_id"]), None)
        elif sql.startswith("SELECT * FROM character_sheet_tasks WHERE id ="):
            self.row = next((r for r in self.store.rows if r["id"] == params[0]), None)
        elif "idempotency_key = %s" in sql:
            self.row = next(
                (
                    r
                    for r in self.store.rows
                    if r["created_by_user_id"] == params[0] and r["idempotency_key"] == params[1]
                ),
                None,
            )
        elif "request_hash = %s" in sql:
            states = {"PENDING", "RUNNING"}
            if "SUBMISSION_UNCERTAIN" in sql:
                states.add("SUBMISSION_UNCERTAIN")
            self.row = next(
                (
                    r
                    for r in self.store.rows
                    if r["created_by_user_id"] == params[0]
                    and r["request_hash"] == params[1]
                    and r["status"] in states
                ),
                None,
            )
        elif sql.startswith("UPDATE character_sheet_tasks"):
            if "SET status = 'SUBMISSION_UNCERTAIN'" in sql:
                for row in self.store.rows:
                    if row["status"] == "RUNNING" and row.get("expired"):
                        row["status"] = "SUBMISSION_UNCERTAIN"
            elif "SET status = 'FAILED'" in sql and "IMAGE_TASK_SUBMISSION_EXPIRED" in sql:
                # UNCERTAIN 超窗自动转失败：fake 里的行都是「刚落库」（无时间
                # 流逝），永不命中 5 分钟窗口；窗口语义由 PG matrix 用例覆盖。
                pass
            elif "SET status = 'RUNNING'" in sql:
                self.row = next((r for r in self.store.rows if r["status"] == "PENDING"), None)
                if self.row:
                    self.row.update(status="RUNNING", attempt=1, locked_by=params[0])
            else:
                raise AssertionError(sql)
        elif sql.startswith("INSERT INTO character_sheet_tasks"):
            fields = (
                "id",
                "project_id",
                "identity_id",
                "created_by_user_id",
                "operation",
                "source_storage_uri",
                "source_content_type",
                "source_sha256",
                "source_size_bytes",
                "idempotency_key",
                "request_hash",
                "request_json",
            )
            row = dict(zip(fields, params, strict=True), status="PENDING")
            self.store.rows.append(row)
            self.rowcount = 1
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self) -> dict[str, Any] | None:
        return self.row

    def commit(self) -> None:
        if self.held:
            self.held = False
            self.store.lock.release()

    rollback = commit


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> AdmissionStore:
    result = AdmissionStore()
    monkeypatch.setattr(
        usage_billing,
        "accept_operation",
        lambda conn, **kwargs: result.reservations.append({**kwargs, "rate_version": "original"}),
    )
    return result


def submit(store: AdmissionStore, key: str, **changes: Any) -> dict[str, Any]:
    conn = AdmissionConnection(store)
    args = dict(
        actor=CurrentUser("owner", "owner", "用户", "employee"),
        operation="CREATE",
        project_id=None,
        identity_id=None,
        display_name="人物",
        persona_name="人物",
        source_storage_uri="fake://input",
        source_content_type="image/png",
        source_sha256="a" * 64,
        source_size_bytes=100,
        idempotency_key=key,
    )
    args.update(changes)
    try:
        return image_tasks.enqueue_character_sheet_task(conn, **args)  # type: ignore[arg-type,return-value]
    finally:
        conn.rollback()


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "SUBMISSION_UNCERTAIN"])
def test_same_semantics_keep_task_and_original_reservation(
    store: AdmissionStore, status: str
) -> None:
    first = submit(store, "original")
    first["status"] = status
    before = [dict(value) for value in store.reservations]
    repeated = submit(store, "another-window-key")
    assert repeated["id"] == first["id"]
    assert store.reservations == before
    assert len(store.rows) == 1


@pytest.mark.parametrize("status", ["FAILED", "SUCCEEDED"])
def test_terminal_replay_and_explicit_new_round_are_distinct(
    store: AdmissionStore, status: str
) -> None:
    first = submit(store, "original")
    first["status"] = status
    assert submit(store, "original")["id"] == first["id"]
    assert len(store.reservations) == 1
    assert submit(store, "explicit-new-round")["id"] != first["id"]
    assert len(store.reservations) == 2


def test_changed_parameters_require_new_key_and_keep_unknown_guard_scoped(
    store: AdmissionStore,
) -> None:
    first = submit(store, "original")
    first["status"] = "SUBMISSION_UNCERTAIN"
    with pytest.raises(HTTPException) as raised:
        submit(store, "original", display_name="不同人物")
    assert raised.value.status_code == 409
    assert submit(store, "new-key", display_name="不同人物")["id"] != first["id"]
    other = CurrentUser("other-owner", "other", "另一用户", "employee")
    assert submit(store, "original", actor=other)["id"] != first["id"]


def test_two_windows_share_one_admission_and_budget(store: AdmissionStore) -> None:
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda key: submit(store, key), ["window-a", "window-b"]))
    assert results[0]["id"] == results[1]["id"]
    assert len(store.rows) == len(store.reservations) == 1


@pytest.mark.parametrize(
    "started,cause,expected",
    [
        (False, OSError("pre-submit"), "FAILED"),
        (True, TimeoutError("after-submit"), "SUBMISSION_UNCERTAIN"),
        (True, HTTPException(502, {"failure_kind": "definitive"}), "FAILED"),
    ],
)
def test_submission_boundary_classifies_retry_safety(
    monkeypatch: pytest.MonkeyPatch, started: bool, cause: Exception, expected: str
) -> None:
    states: list[str] = []

    class FailureConnection:
        def execute(self, sql: str, params: tuple[Any, ...]) -> Any:
            if "SET status = %s" in sql:
                states.append(params[0])
            return SimpleNamespace(fetchone=lambda: {"result_json": None}, rowcount=1)

        def commit(self) -> None:
            pass

    monkeypatch.setattr(usage_billing, "finish_source", lambda *args, **kwargs: None)
    image_tasks.fail_image_task(
        FailureConnection(),
        table="character_sheet_tasks",  # type: ignore[arg-type]
        lease=image_tasks.ImageTaskLease("task", "owner", "worker", 1),
        cause=cause,
        submission_started=started,
    )
    assert states == [expected]


def test_expired_lease_stays_unknown_and_cannot_start_another_paid_attempt(
    store: AdmissionStore,
) -> None:
    first = submit(store, "original")
    first.update(status="RUNNING", expired=True, attempt=1)
    lease = image_tasks.acquire_character_sheet_task(AdmissionConnection(store), worker_id="next")  # type: ignore[arg-type]
    assert lease is None
    assert first["status"] == "SUBMISSION_UNCERTAIN"
    assert submit(store, "after-reload")["id"] == first["id"]
    assert len(store.reservations) == 1


def test_coalesced_key_replays_after_lost_response_and_original_completion(
    store: AdmissionStore,
) -> None:
    first = submit(store, "window-a")
    assert submit(store, "window-b-lost-response")["id"] == first["id"]
    first["status"] = "SUCCEEDED"
    # 窗口 B 没收到入队响应，恢复时只有原 key，必须继续指向 A 的已完成任务。
    assert submit(store, "window-b-lost-response")["id"] == first["id"]
    assert len(store.rows) == len(store.reservations) == 1
    with pytest.raises(HTTPException):
        submit(store, "window-b-lost-response", display_name="变更参数")
    # 已看见结果后的明确新一轮仍使用新 key，不永久按指纹复用成功结果。
    assert submit(store, "explicit-new-operation")["id"] != first["id"]


@pytest.mark.parametrize("has_nonprimary_alias", [True, False])
def test_alias_migration_refuses_lossy_downgrade(has_nonprimary_alias: bool) -> None:
    path = Path(__file__).parents[1] / "migrations/versions/20261012T0200_character_request_keys.py"
    spec = importlib.util.spec_from_file_location("character_request_key_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    dropped: list[str] = []
    queried: list[str] = []

    def query(statement: Any) -> Any:
        queried.append(str(statement))
        return SimpleNamespace(scalar=lambda: has_nonprimary_alias)

    migration.op = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=query),
        execute=dropped.append,
    )
    if has_nonprimary_alias:
        with pytest.raises(RuntimeError, match="不能删除防重记录"):
            migration.downgrade()
        assert not any(statement.startswith("DROP") for statement in dropped)
    else:
        migration.downgrade()
        assert dropped[-1] == "DROP TABLE character_sheet_task_requests"
    assert "k.idempotency_key <> t.idempotency_key" in queried[0]
