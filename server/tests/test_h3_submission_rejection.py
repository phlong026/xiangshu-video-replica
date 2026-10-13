"""上游明确拒绝提交（HTTP 402 等）必须是确定性失败，不能落入 SUBMISSION_UNCERTAIN。

2026-10-13 事故：H3 余额不足时上游返回 HTTP 402，请求没有被受理、没有扣费，
但提交路径把任何 H3ProviderFailed 都转成 SubmissionUncertain，任务因此永远占着
账号并发名额和暂扣积分。本文件锁定新的判定：

- 白名单状态码（400/401/402/403/404/422）= 明确拒绝 → 终态失败、释放暂扣、成本记 0；
- 408/429/5xx、网络错误、响应解析失败 = 可能已受理 → 仍然是 SubmissionUncertain。
"""

# 夹具（fair_state）从 test_worker_crash_recovery 导入后作为参数使用，ruff 会误报 F811。
# ruff: noqa: F811

from __future__ import annotations

import io
import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.error import HTTPError

os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-t26-worker-reliability-tests-minimum-48-bytes-long-12",
)

import psycopg
import pytest
from test_worker_crash_recovery import (  # noqa: F401  (夹具与辅助函数)
    _billing_rows,
    _cursor_count,
    _seed,
    fair_dsn,
    fair_state,
)

from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.generation import (
    H3ProviderFailed,
    MetasoH3Provider,
    SubmissionUncertain,
    UrllibMetasoHttpTransport,
    build_h3_request,
)
from app.generation_worker import run_pg_worker_once
from app.internal_billing import reserve_internal_billing
from app.storage import FakeStorageAdapter

REJECTED_STATUSES = [400, 401, 402, 403, 404, 422]
AMBIGUOUS_STATUSES = [408, 429, 500, 502, 503, 504]

PROVIDER_BODY = json.dumps(
    {"error": {"code": "insufficient_balance_error", "message": "H3 积分余额不足 (1008)"}},
    ensure_ascii=False,
).encode()


def _request() -> dict[str, Any]:
    return build_h3_request(
        prompt_text="虚构提示词",
        first_frame_url="https://example.com/frame.png",
        duration_seconds=5,
        resolution="768P",
    )


class _RaisingTransport:
    """submit 阶段的 POST 直接抛出给定异常。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        raise self.exc


class _ReturningTransport:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        return self.payload


# ---------------------------------------------------------------------------
# 传输层：HTTP 状态码与上游原话必须带到异常上
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", REJECTED_STATUSES + AMBIGUOUS_STATUSES)
def test_transport_keeps_http_status_on_the_exception(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise HTTPError("https://fake.test/x", status, "fake", None, io.BytesIO(PROVIDER_BODY))

    monkeypatch.setattr("app.generation.recorded_urlopen", fail)

    with pytest.raises(H3ProviderFailed) as caught:
        UrllibMetasoHttpTransport().request("POST", "https://fake.test/x", headers={})

    assert caught.value.http_status == status
    assert caught.value.provider_reason is not None
    assert "H3 积分余额不足" in caught.value.provider_reason


def test_transport_network_failure_has_no_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("timed out")

    monkeypatch.setattr("app.generation.recorded_urlopen", fail)

    with pytest.raises(H3ProviderFailed) as caught:
        UrllibMetasoHttpTransport().request("POST", "https://fake.test/x", headers={})

    assert caught.value.http_status is None


# ---------------------------------------------------------------------------
# provider.submit_image_to_video：确定性拒绝 vs 可能已受理
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", REJECTED_STATUSES)
def test_submit_explicit_rejection_is_a_definite_failure(status: int) -> None:
    transport = _RaisingTransport(
        H3ProviderFailed("rejected", http_status=status, provider_reason="虚构拒绝原因")
    )
    provider = MetasoH3Provider(api_key="k", transport=transport)

    with pytest.raises(H3ProviderFailed) as caught:
        provider.submit_image_to_video(_request())

    assert not isinstance(caught.value, SubmissionUncertain)
    assert caught.value.http_status == status
    assert caught.value.provider_task_id is None


@pytest.mark.parametrize("status", AMBIGUOUS_STATUSES)
def test_submit_ambiguous_http_status_stays_uncertain(status: int) -> None:
    transport = _RaisingTransport(H3ProviderFailed("ambiguous", http_status=status))
    provider = MetasoH3Provider(api_key="k", transport=transport)

    with pytest.raises(SubmissionUncertain):
        provider.submit_image_to_video(_request())


def test_submit_network_failure_stays_uncertain() -> None:
    transport = _RaisingTransport(H3ProviderFailed("H3 provider request failed"))
    provider = MetasoH3Provider(api_key="k", transport=transport)

    with pytest.raises(SubmissionUncertain):
        provider.submit_image_to_video(_request())


def test_submit_unparseable_success_response_stays_uncertain() -> None:
    provider = MetasoH3Provider(api_key="k", transport=_ReturningTransport(b"<html>oops</html>"))

    with pytest.raises(SubmissionUncertain):
        provider.submit_image_to_video(_request())


# ---------------------------------------------------------------------------
# worker：终态失败 + 释放暂扣 + 释放名额 + 成本记 0 + 客户文案不泄露供应商余额
# ---------------------------------------------------------------------------


class _SubmitRaisingProvider(MetasoH3Provider):
    def __init__(self, exc: Exception) -> None:
        super().__init__(api_key="test-key")
        self.exc = exc

    def submit_image_to_video(self, request: dict[str, Any]) -> str:
        raise self.exc


def _run_one(dsn: str, provider: MetasoH3Provider) -> None:
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    assert (
        run_pg_worker_once(
            worker_id="rejection-test",
            storage=storage,
            generation_provider=provider,
            max_tasks=1,
        )
        == 1
    )


def _task(dsn: str, task_id: str) -> dict[str, Any]:
    with psycopg.connect(dsn) as raw:
        cursor = raw.execute(
            "SELECT status, error_code, error_message_redacted, actual_cost, cost_status, "
            "provider_task_id, locked_by FROM generation_tasks WHERE id = %s",
            (task_id,),
        )
        names = [column.name for column in cursor.description or []]
        return dict(zip(names, cursor.fetchone() or [], strict=True))


def _reserve(task_id: str) -> None:
    """真实暂扣（billing_operations + RESERVE 流水 + 钱包移动），与生产创建批次后的状态一致。"""
    with pg_transaction() as raw:
        reserve_internal_billing(
            BusinessConnection.postgres(raw), user_id="u1", task_id=task_id, billing_round=1
        )


def _wallet_credits(dsn: str, user_id: str) -> tuple[int, int]:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


def _batch_status(dsn: str, task_id: str) -> str:
    with psycopg.connect(dsn) as raw:
        row = raw.execute(
            "SELECT b.status FROM generation_batches b "
            "JOIN generation_tasks t ON t.batch_id = b.id WHERE t.id = %s",
            (task_id,),
        ).fetchone()
    return str(row[0])


def test_worker_rejected_submission_fails_releases_and_hides_provider_balance(
    fair_state: str,
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    _reserve("task-u1-0")
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    assert _wallet_credits(fair_state, "u1") == (999, 1)
    provider = _SubmitRaisingProvider(
        H3ProviderFailed(
            "H3 provider returned HTTP 402",
            http_status=402,
            provider_reason="H3 积分余额不足 (1008)（insufficient_balance_error）",
        )
    )

    _run_one(fair_state, provider)

    task = _task(fair_state, "task-u1-0")
    assert task["status"] == "FAILED"
    assert task["error_code"] == "H3_SUBMISSION_REJECTED"
    assert task["provider_task_id"] is None
    assert task["locked_by"] is None
    # 客户可见文案：说明未扣费，但不暴露供应商余额状态与内部错误码。
    message = str(task["error_message_redacted"])
    assert "未扣费" in message
    assert "余额" not in message and "1008" not in message and "insufficient" not in message
    # 请求被拒绝，没有产生任何费用。
    assert task["actual_cost"] == 0
    assert task["cost_status"] == "ACTUAL"
    # 暂扣释放、名额释放、批次收口。
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1), ("RELEASE", 1)]
    assert _wallet_credits(fair_state, "u1") == (1000, 0)  # 全额退回，没有结算
    assert _cursor_count(fair_state, "u1") == 0
    # 批次口径沿用既有的状态词汇：单任务批次的任务失败即「完成但有失败」。
    assert _batch_status(fair_state, "task-u1-0") == "COMPLETED_WITH_FAILURES"


def test_worker_ambiguous_submission_still_becomes_uncertain(fair_state: str) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    _reserve("task-u1-0")
    provider = _SubmitRaisingProvider(
        SubmissionUncertain("H3 provider submission result is unknown")
    )

    _run_one(fair_state, provider)

    task = _task(fair_state, "task-u1-0")
    assert task["status"] == "SUBMISSION_UNCERTAIN"
    assert task["error_code"] == "SUBMISSION_UNCERTAIN"
    # 可能已受理：暂扣必须保留到对账确认，不能自动释放。
    assert _billing_rows(fair_state, "task-u1-0") == [("RESERVE", 1)]
    assert _wallet_credits(fair_state, "u1") == (999, 1)


def test_worker_provider_failure_without_http_status_keeps_legacy_behavior(
    fair_state: str,
) -> None:
    _seed(fair_state, user_ids=["u1"], tasks_per_user=1, wallet_credits=1000)
    provider = _SubmitRaisingProvider(H3ProviderFailed("generic", provider_reason="虚构失败原因"))

    _run_one(fair_state, provider)

    task = _task(fair_state, "task-u1-0")
    # 没有 HTTP 状态码的失败不属于「明确拒绝」，维持原有的失败语义与文案。
    assert task["status"] == "FAILED"
    assert task["error_code"] == "H3_PROVIDER_FAILED"
    assert "本次未扣费" not in str(task["error_message_redacted"])
