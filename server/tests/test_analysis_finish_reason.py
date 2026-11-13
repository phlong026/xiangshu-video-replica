"""终止原因分类、调用预算与对外中性化：拆解稳定性改造（方案 T2/T3/T4/T5/T14）。

背景：网关把「输出截断」「安全拦截」「空结果」混成同一种空返回，用户重试
徒劳、排障靠猜。2026-11-11 起响应终止原因先于正文可用性判定，视频入口
启用截止预算与有界重试，优化器显式钉死模型不受拆解换代波及。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import Mock

import pytest

from app import analysis_routes, prompt_optimizer_routes
from app.analysis import (
    ANALYSIS_MAX_TOKENS_CAP,
    ANALYSIS_MIN_MAX_TOKENS,
    HTTP_FAILURE_PHASE,
    NETWORK_FAILURE_PHASE,
    AnalysisProviderFailed,
    ApilioGemini,
    VideoCallBudget,
    _analysis_max_tokens,
    _with_failover_note,
)
from app.prompt_optimizer_routes import PROMPT_OPTIMIZER_MODEL

PRIMARY = "gemini-4-carbon"
BACKUP = "gemini-3.8-flash"
# 截止预算测试用：每次重试停顿把假时钟推进「退避秒数 + 300」，模拟真实耗时。
FAKE_PAUSE_ADVANCE = 300


def _rate_limited() -> AnalysisProviderFailed:
    return AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 429）",
        http_status=429,
        failure_phase=HTTP_FAILURE_PHASE,
        retryable=True,
    )


def _network_failure(*, kind: str) -> AnalysisProviderFailed:
    return AnalysisProviderFailed(
        f"network failure ({kind})",
        failure_phase=NETWORK_FAILURE_PHASE,
        retryable=kind == "connect",
        upstream_diagnostic={
            "http_status": None,
            "failure_phase": NETWORK_FAILURE_PHASE,
            "reason": type("Exc", (), {}).__name__ if kind == "connect" else "TimeoutError",
            "failure_kind": kind,
        },
    )


def _completion_body(
    *,
    content: str | None,
    finish_reason: str | int | None = None,
    usage: dict[str, Any] | None = None,
    include_message: bool = True,
) -> bytes:
    choice: dict[str, Any] = {}
    if include_message:
        choice["message"] = {"content": content}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    payload: dict[str, Any] = {"id": "resp-1", "choices": [choice]}
    if usage is not None:
        payload["usage"] = usage
    return json.dumps(payload).encode()


class _ScriptedTransport:
    """按脚本回放响应/异常，记录每次请求的模型名与完整请求体。"""

    timeout_seconds: float | None = None

    def __init__(self, script: list[bytes | Exception]) -> None:
        self._pending = list(script)
        self.requested_models: list[str] = []
        self.requested_payloads: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: Any, body: bytes) -> tuple[bytes, dict[str, str]]:
        payload = json.loads(body.decode("utf-8"))
        assert isinstance(payload.get("model"), str) and payload["model"]
        self.requested_models.append(payload["model"])
        self.requested_payloads.append(payload)
        outcome = self._pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, {}


def _provider(
    script: list[bytes | Exception],
    *,
    model: str = PRIMARY,
    fallback_models: tuple[str, ...] = (BACKUP,),
    video_call_budget: VideoCallBudget | None = None,
    pause: Any = None,
) -> tuple[ApilioGemini, _ScriptedTransport]:
    transport = _ScriptedTransport(script)
    provider = ApilioGemini(
        api_key="k",
        model=model,
        fallback_models=fallback_models,
        transport=transport,
        failover_pause=(lambda _seconds: None) if pause is None else pause,
        video_call_budget=video_call_budget,
    )
    return provider, transport


# ---------------------------------------------------------------------------
# 终止原因分类（T2）：先于正文可用性判定
# ---------------------------------------------------------------------------


def test_truncated_body_is_rejected_even_when_content_present() -> None:
    """length + 非空正文：半截 JSON 不许流到业务解析层变成笼统「格式无效」。"""
    provider, transport = _provider(
        [_completion_body(content='{"analysis": {"sho', finish_reason="length")],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]
    assert caught.value.retryable is False
    assert (caught.value.upstream_diagnostic or {})["reason"] == "finish_reason_length"


def test_truncation_without_budget_fails_closed() -> None:
    provider, transport = _provider(
        [_completion_body(content='{"analysis"', finish_reason="length")]
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]
    assert caught.value.retryable is False
    assert (caught.value.upstream_diagnostic or {})["reason"] == "finish_reason_length"
    assert "长度上限" in str(caught.value)


def test_content_filter_with_nonempty_body_is_rejected() -> None:
    """拦截正文可能把拦截原因漏成一次「成功」，正文存在也必须判失败。"""
    provider, transport = _provider(
        [
            _completion_body(content="这段视频无法展示", finish_reason="content_filter"),
            _completion_body(content='{"ok": 1}'),
        ]
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]
    assert caught.value.retryable is False
    assert (caught.value.upstream_diagnostic or {})["reason"] == "finish_reason_content_filter"
    assert "安全策略" in str(caught.value)


@pytest.mark.parametrize("reason", ["safety", "recitation", "prohibited_content"])
def test_safety_family_reasons_are_all_rejected(reason: str) -> None:
    provider, _ = _provider([_completion_body(content="x", finish_reason=reason)])

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert (caught.value.upstream_diagnostic or {})["reason"] == "finish_reason_content_filter"


def test_stop_with_empty_content_is_retryable_empty_completion() -> None:
    provider, _ = _provider([_completion_body(content="", finish_reason="stop")])

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert caught.value.retryable is True
    diagnostic = caught.value.upstream_diagnostic or {}
    assert diagnostic["reason"] == "empty_completion"
    assert diagnostic["finish_reason"] == "stop"


def test_missing_finish_reason_keeps_historical_content_checks() -> None:
    """历史信封没有终止原因字段：合法正文照常成功（向后兼容）。"""
    provider, _ = _provider([_completion_body(content='{"ok": 1}')])

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert response.raw["finish_reason"] is None


def test_non_string_finish_reason_is_treated_as_unknown() -> None:
    provider, _ = _provider([_completion_body(content='{"ok": 1}', finish_reason=123)])

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert response.text == '{"ok": 1}'
    assert response.raw["finish_reason"] is None


def test_missing_message_with_unknown_reason_is_unreadable() -> None:
    provider, _ = _provider([_completion_body(content=None, include_message=False)])

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert (caught.value.upstream_diagnostic or {})["reason"] == "missing_content"


def test_missing_message_does_not_short_circuit_finish_reason() -> None:
    """连 message 都缺失的拦截响应也必须归到明确原因（评审变更集 #4）。"""
    body = json.dumps({"choices": [{"finish_reason": "content_filter"}]}).encode()
    provider, _ = _provider([body])

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert (caught.value.upstream_diagnostic or {})["reason"] == "finish_reason_content_filter"
    assert caught.value.retryable is False


def test_success_metadata_carries_finish_reason_and_whitelisted_usage() -> None:
    """成功 ref 只外带白名单 usage 数值；未知名段与非法类型不外泄（T3）。"""
    provider, _ = _provider(
        [
            _completion_body(
                content='{"ok": 1}',
                finish_reason="stop",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 7,
                    "total_tokens": 107,
                    "cached_tokens": 55,
                    "reasoning_tokens": "many",
                },
            )
        ]
    )

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert response.raw["finish_reason"] == "stop"
    assert response.raw["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 7,
        "total_tokens": 107,
    }


# ---------------------------------------------------------------------------
# 动态输出预算（T5）
# ---------------------------------------------------------------------------


def test_max_tokens_scale_with_duration_and_cap() -> None:
    assert _analysis_max_tokens(4) == ANALYSIS_MIN_MAX_TOKENS + 4 * 800
    assert _analysis_max_tokens(15) == ANALYSIS_MIN_MAX_TOKENS + 15 * 800
    assert _analysis_max_tokens(20) == ANALYSIS_MAX_TOKENS_CAP
    assert _analysis_max_tokens(30) == ANALYSIS_MAX_TOKENS_CAP
    assert _analysis_max_tokens(180) == ANALYSIS_MAX_TOKENS_CAP
    assert _analysis_max_tokens(0.5) == ANALYSIS_MIN_MAX_TOKENS


def test_video_entries_carry_dynamic_output_budget() -> None:
    provider, transport = _provider([_completion_body(content='{"ok": 1}')])

    provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_payloads[0]["max_tokens"] == ANALYSIS_MIN_MAX_TOKENS + 15 * 800


def test_output_budget_rejected_by_upstream_falls_back_once() -> None:
    """请求期 400 且原因指向输出上限：回退保守预算重试一次。"""
    rejected = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）：max_tokens exceeds the model output limit",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic={"reason": "max_tokens exceeds the model output limit"},
    )
    provider, transport = _provider(
        [rejected, _completion_body(content='{"ok": 1}')],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=30.0)

    budgets = [payload["max_tokens"] for payload in transport.requested_payloads]
    assert budgets == [ANALYSIS_MAX_TOKENS_CAP, ANALYSIS_MIN_MAX_TOKENS]
    assert response.text == '{"ok": 1}'


def test_input_token_limit_does_not_trigger_output_budget_fallback() -> None:
    """输入超限降输出预算救不了：不得误触发回退（评审变更集 #5）。"""
    rejected = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）：Input token limit exceeded: prompt is too long",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic={"reason": "Input token limit exceeded: prompt is too long"},
    )
    provider, transport = _provider(
        [rejected],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    with pytest.raises(AnalysisProviderFailed):
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert len(transport.requested_payloads) == 1
    assert transport.requested_payloads[0]["max_tokens"] == ANALYSIS_MIN_MAX_TOKENS + 15 * 800


def test_unrelated_http_400_does_not_touch_the_budget() -> None:
    rejected = AnalysisProviderFailed(
        "视频拆解服务拒绝了请求（HTTP 400）：invalid video URL",
        http_status=400,
        failure_phase=HTTP_FAILURE_PHASE,
        upstream_diagnostic={"reason": "invalid video URL"},
    )
    provider, transport = _provider([rejected])

    with pytest.raises(AnalysisProviderFailed):
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=30.0)

    assert len(transport.requested_payloads) == 1
    assert transport.requested_payloads[0]["max_tokens"] == ANALYSIS_MAX_TOKENS_CAP


# ---------------------------------------------------------------------------
# 调用预算（T4）：截止时间、全局次数、同模型有界重试
# ---------------------------------------------------------------------------


def test_rate_limited_retries_same_model_before_falling_over() -> None:
    """预算内 429 先同模型重试，恢复后不再消耗备选模型。"""
    provider, transport = _provider(
        [_rate_limited(), _rate_limited(), _completion_body(content='{"ok": 1}')],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY, PRIMARY, PRIMARY]
    assert response.raw["model"] == PRIMARY


def test_global_call_cap_limits_total_provider_requests() -> None:
    """物理请求总数封顶：主模型重试用尽后换道，备选只拿到最后一个名额。"""
    provider, transport = _provider(
        [_rate_limited(), _rate_limited(), _rate_limited(), _rate_limited(), _rate_limited()],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert len(transport.requested_models) == 4
    assert transport.requested_models[:3] == [PRIMARY, PRIMARY, PRIMARY]
    assert transport.requested_models[3] == BACKUP
    assert caught.value.retryable is True


def test_budget_deadline_stops_new_calls_after_slow_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """截止预算：一次满额调用后剩余预算不足，不再发起任何新请求。"""
    from app import analysis as analysis_module

    clock = [100.0]
    monkeypatch.setattr(analysis_module.time, "monotonic", lambda: clock[0])
    provider, transport = _provider(
        [_rate_limited()],
        video_call_budget=VideoCallBudget(attempt_deadline=600.0),
        pause=lambda _seconds: clock.__setitem__(0, clock[0] + FAKE_PAUSE_ADVANCE),
    )
    transport.timeout_seconds = 240.0

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    # 100s 起算 + 500s 预算；首次调用后时钟推进 302s，剩余 298s 已不足
    # 一次 240s 的完整调用（402+240=642 > 600 截止线）。
    assert transport.requested_models == [PRIMARY]
    assert caught.value.http_status == 429


def test_tiny_budget_below_one_timeout_fails_before_any_call() -> None:
    """预算小于单次超时的防御路径：零请求即失败，不产生半次付费调用。"""
    provider, transport = _provider(
        [_completion_body(content='{"ok": 1}')],
        video_call_budget=VideoCallBudget(attempt_deadline=__import__("time").monotonic() + 10),
    )
    transport.timeout_seconds = 240.0

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == []
    assert caught.value.failure_phase == "request"


def test_timeout_failures_are_never_auto_retried() -> None:
    """超时可能已被上游受理并计费：同模型与备选都不自动重发（决策 D5）。"""
    provider, transport = _provider(
        [_network_failure(kind="timeout"), _completion_body(content='{"ok": 1}')],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]
    assert caught.value.upstream_diagnostic is not None
    assert caught.value.upstream_diagnostic["failure_kind"] == "timeout"


def test_connect_failures_are_not_auto_retried() -> None:
    """断连可能发生在请求已提交之后：一律不自动重发（评审变更集 #1）。"""
    provider, transport = _provider(
        [_network_failure(kind="connect"), _completion_body(content='{"ok": 1}')],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY]
    assert caught.value.upstream_diagnostic is not None
    assert caught.value.upstream_diagnostic["failure_kind"] == "connect"


def test_safety_and_length_failures_consume_no_retry_and_no_fallback() -> None:
    """截断/拦截永不重试也不换道：重发与换模型都救不了这两类失败。"""
    for body in (
        _completion_body(content="x", finish_reason="length"),
        _completion_body(content="x", finish_reason="content_filter"),
    ):
        provider, transport = _provider(
            [body, _completion_body(content='{"ok": 1}')],
            video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
        )

        with pytest.raises(AnalysisProviderFailed):
            provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

        assert transport.requested_models == [PRIMARY]


def test_empty_completion_retries_within_budget() -> None:
    provider, transport = _provider(
        [
            _completion_body(content="", finish_reason="stop"),
            _completion_body(content=""),
            _completion_body(content='{"ok": 1}'),
        ],
        video_call_budget=VideoCallBudget(attempt_deadline=float("inf")),
    )

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY, PRIMARY, PRIMARY]
    assert response.text == '{"ok": 1}'


def test_provider_without_budget_keeps_legacy_single_attempt_per_model() -> None:
    """未启用预算的入口（优化器等）：一次请求、按既有语义换道，绝不重试。"""
    provider, transport = _provider([_rate_limited(), _completion_body(content='{"ok": 1}')])

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert transport.requested_models == [PRIMARY, BACKUP]
    assert response.raw["model"] == BACKUP


# ---------------------------------------------------------------------------
# 对外文案与中性化（T7/T7b）
# ---------------------------------------------------------------------------


def test_provider_error_appends_failover_note_for_rate_limit() -> None:
    failure = _with_failover_note(_rate_limited(), [PRIMARY, BACKUP])

    http_error = analysis_routes.analysis_provider_error(failure)

    detail = http_error.detail
    assert isinstance(detail, dict)
    message = str(detail["message"])
    assert "限流" in message
    assert PRIMARY in message and BACKUP in message


def test_external_version_payload_neutralizes_provider_name() -> None:
    payload: dict[str, Any] = {
        "schema_version": "b3.analysis.v1",
        "provider_response_ref": {
            "stored_as": "versions.payload_json",
            "raw": {"provider": "apilio_gemini", "model": PRIMARY},
        },
        "analysis": {"shots": []},
    }

    external = analysis_routes.external_version_payload(payload)

    ref = external["provider_response_ref"]
    assert isinstance(ref, dict)
    raw = ref["raw"]
    assert isinstance(raw, dict)
    assert raw["provider"] == "analysis_gateway"
    assert raw["model"] == PRIMARY
    assert payload["provider_response_ref"]["raw"]["provider"] == "apilio_gemini"  # 原对象不动


def test_external_version_payload_passthrough_without_ref() -> None:
    assert analysis_routes.external_version_payload({"x": 1}) == {"x": 1}


# ---------------------------------------------------------------------------
# 优化器钉死模型（T14）
# ---------------------------------------------------------------------------


def test_prompt_optimizer_provider_pins_its_own_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """优化器不跟随拆解默认值换代，也不启用视频调用预算。"""
    repository = Mock()
    repository.load_provider_config.side_effect = lambda provider_name: (
        {"analysis_api_key": "k"} if provider_name == "apilio" else {}
    )
    monkeypatch.setattr(prompt_optimizer_routes, "SettingsRepository", lambda conn: repository)

    provider = prompt_optimizer_routes.get_prompt_optimizer_provider(Mock())

    assert isinstance(provider, ApilioGemini)
    # D9 后主链路出厂值与优化器同为 flash：钉死的意义在「不跟随设置页变化」，
    # 由显式常量保证；主备链路各自的出厂值在模型配置测试里另行钉死。
    assert provider.model == PROMPT_OPTIMIZER_MODEL
    assert provider.model == "gemini-3.8-flash"
    assert provider.video_call_budget is None


# ---------------------------------------------------------------------------
# 网关换行注入兜底（T15，2026-11-12 Phase 0 实测发现）
# ---------------------------------------------------------------------------


def _body_with_injected_newlines(*, mid_multibyte: bool = False) -> bytes:
    """复刻网关流式分块的注入形态：\\r\\n 落进键名内部或汉字字节中间."""
    if mid_multibyte:
        raw = json.dumps(
            {
                "id": "resp-1",
                "choices": [{"message": {"content": '{"ok": "好"}'}, "finish_reason": "stop"}],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        needle = "好".encode()
        index = raw.index(needle)
        # 在多字节汉字（e5 a5 bd）的第一个字节后插入 \r\n：裸解码必失败，
        # 0x0D/0x0A 不在 UTF-8 续字节范围内，字节级剔除后应完整恢复。
        return raw[: index + 1] + b"\r\n" + raw[index + 1 :]
    body = _completion_body(content='{"ok": 1}', finish_reason="stop")
    return body.replace(b'"finish_reason"', b'"finish_reason\r\n"', 1)


def test_injected_newlines_in_completion_body_are_sanitized() -> None:
    provider, _ = _provider([_body_with_injected_newlines()])

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert response.text == '{"ok": 1}'
    assert response.raw["salvaged"] is True
    assert response.raw["finish_reason"] == "stop"


def test_clean_body_is_not_marked_salvaged() -> None:
    provider, _ = _provider([_completion_body(content='{"ok": 1}', finish_reason="stop")])

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert response.raw["salvaged"] is False


def test_newlines_injected_inside_a_multibyte_char_are_safe() -> None:
    """注入落在多字节字符字节中间时，字节级剔除仍能完整恢复正文。"""
    provider, _ = _provider([_body_with_injected_newlines(mid_multibyte=True)])

    response = provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert response.text == '{"ok": "好"}'
    assert response.raw["salvaged"] is True


def test_garbage_completion_body_still_fails_closed() -> None:
    """剔除换行救不了真正的坏响应：仍然按不可读补全失败，不静默放行。"""
    transport = _ScriptedTransport([b"not a json body"])
    provider = ApilioGemini(
        api_key="k",
        model=PRIMARY,
        fallback_models=(),
        transport=transport,
        failover_pause=lambda _seconds: None,
    )

    with pytest.raises(AnalysisProviderFailed) as caught:
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)

    assert (caught.value.upstream_diagnostic or {})["reason"] == "JSONDecodeError"


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        _completion_body(content="partial", finish_reason="length"),
        _completion_body(content="blocked", finish_reason="content_filter"),
        _completion_body(content="", include_message=False),
        _completion_body(content=""),
    ],
)
def test_completion_diagnostic_logs_are_provider_neutral(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    provider, _ = _provider([body], fallback_models=())
    with pytest.raises(AnalysisProviderFailed):
        provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)
    assert caplog.records
    assert "apilio" not in caplog.text.lower()
    assert "gemini" not in caplog.text.lower()


def test_long_video_timeout_leaves_room_for_provider_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import replica_prompt
    from app.analysis import _BudgetTracker

    monkeypatch.setenv(replica_prompt.REPLICA_PROMPT_TIMEOUT_ENV, "99999")
    monkeypatch.setattr(replica_prompt, "get_video_analysis_provider", lambda conn, **kw: kw)
    monkeypatch.setattr(replica_prompt.time, "monotonic", lambda: 100.0)
    options = replica_prompt.build_long_video_provider(Mock())
    monkeypatch.setattr(replica_prompt.time, "monotonic", lambda: 101.0)
    assert _BudgetTracker(options["video_call_budget"]).can_start(
        options["transport"].timeout_seconds
    )
    # 准备时间超限时仍拒绝付费请求，不延长截止线。
    monkeypatch.setattr(replica_prompt.time, "monotonic", lambda: 221.0)
    assert not _BudgetTracker(options["video_call_budget"]).can_start(
        options["transport"].timeout_seconds
    )


@pytest.mark.parametrize(
    ("raw", "expected"), [("", 900.0), ("bad", 900.0), ("1", 60.0), ("900", 900.0)]
)
def test_long_video_timeout_preserves_default_and_lower_bound(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    from app import replica_prompt

    monkeypatch.setenv(replica_prompt.REPLICA_PROMPT_TIMEOUT_ENV, raw)
    assert replica_prompt.provider_timeout_seconds() == expected
