"""空壳响应的归类与处置（2026-11-20 报障 F82C66E9）。

生产事实：视频拆解首调 HTTP 200、finish_reason=stop，但 content 只有
``{"analysis": {}}``（5911 prompt → 41 completion，10.8s；同日在跑的正常
调用耗时 37–39s）。当时把它当「格式错误」去重问「保持内容与字段完全不变，
只修格式」，模型只能把空壳抄成空字符串 + ``shots: []``（153 → 135 token），
二次校验依旧失败，对外还报「格式无效」。

本文件锁定三条契约：
1. 空内容单独归类（reason=empty_analysis，可重试），文案与事实一致；
2. 空结果不发起付费格式修复——那样只会把空内容再抄一遍；
3. 有内容但结构残缺的响应仍走原修复通道，不被误判成空壳。

两条样本是生产 external_call_logs 的原始响应，原样落盘、未做任何改写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.analysis import (
    AnalysisProviderFailed,
    ApilioGemini,
    ProviderResponse,
    analyze_video,
)

FIXTURES = Path(__file__).parent / "fixtures" / "golden_analysis"
EMPTY_ENVELOPE = FIXTURES / "empty_f82c66e9_envelope.json"
EMPTY_AFTER_REPAIR = FIXTURES / "empty_after_repair_f82c66e9_envelope.json"
DURATION = 20.689002


def _content(path: Path) -> str:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    choice = envelope["choices"][0]
    assert choice["finish_reason"] == "stop"
    return choice["message"]["content"]


class _ScriptedProvider:
    """编排「首调 / 格式修复」两段文本，并记录修复是否被调用。"""

    requires_https_video_url = True

    def __init__(self, *, first: str, repaired: str) -> None:
        self.first = first
        self.repaired = repaired
        self.repair_calls = 0

    def analyze(self, *, video_uri: str, duration_seconds: float) -> ProviderResponse:
        return ProviderResponse(text=self.first, raw={"provider": "scripted"})

    def repair_json(
        self, *, broken_text: str, error_message: str, max_tokens: int
    ) -> ProviderResponse:
        self.repair_calls += 1
        assert broken_text == self.first
        return ProviderResponse(text=self.repaired, raw={"provider": "scripted"})


def _run(provider: _ScriptedProvider) -> AnalysisProviderFailed:
    with pytest.raises(AnalysisProviderFailed) as caught:
        analyze_video(
            video_uri="https://example.com/source.mp4",
            video_duration_seconds=DURATION,
            provider=provider,
        )
    return caught.value


def test_empty_envelope_is_reported_as_empty_result_without_paid_repair() -> None:
    """首调就是空壳：按「上游没给内容」归类，且绝不发起修复重问。"""
    provider = _ScriptedProvider(first=_content(EMPTY_ENVELOPE), repaired="{}")

    failure = _run(provider)

    assert "未返回有效分析内容" in str(failure)
    assert "格式无效" not in str(failure)
    assert failure.retryable is True
    assert (failure.upstream_diagnostic or {}).get("reason") == "empty_analysis"
    assert provider.repair_calls == 0


def test_repair_that_reproduces_the_empty_shell_is_classified_as_empty() -> None:
    """首调是坏 JSON、修复后仍是空壳：仍按空结果归类，而不是「修复未成功」。"""
    provider = _ScriptedProvider(first="{not a valid json", repaired=_content(EMPTY_AFTER_REPAIR))

    failure = _run(provider)

    assert provider.repair_calls == 1
    assert "未返回有效分析内容" in str(failure)
    assert "格式无效" not in str(failure)
    assert (failure.upstream_diagnostic or {}).get("reason") == "empty_analysis_after_repair"


def test_payload_with_content_but_missing_shots_keeps_the_repair_path() -> None:
    """有内容但结构残缺不算空壳：必须继续走原修复通道（不误伤）。"""
    partial = json.dumps(
        {"analysis": {"summary": "有摘要但没有分镜", "shots": []}}, ensure_ascii=False
    )
    provider = _ScriptedProvider(first=partial, repaired=partial)

    failure = _run(provider)

    assert provider.repair_calls == 1
    assert "自动修复未成功" in str(failure)


class _RecordingTransport:
    timeout_seconds: float | None = None

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: Any, body: bytes) -> tuple[bytes, dict[str, str]]:
        self.payloads.append(json.loads(body.decode("utf-8")))
        response = {
            "id": "resp-1",
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return json.dumps(response).encode(), {}


def test_both_video_entries_request_json_object() -> None:
    """视频入口固定 json_object：信封 schema 已撤回，防止回归重引入空壳。"""
    transport = _RecordingTransport()
    provider = ApilioGemini(
        api_key="k",
        model="gemini-3.8-flash",
        transport=transport,  # type: ignore[arg-type]
        failover_pause=lambda _seconds: None,
    )

    provider.analyze(video_uri="https://example.com/v.mp4", duration_seconds=15.0)
    provider.analyze_with_context(
        video_uri="https://example.com/v.mp4",
        duration_seconds=15.0,
        context={
            "mode": None,
            "duration_seconds": 15.0,
            "generation_assets": [],
            "issues": [],
            "media_info": {},
        },
        media=[],
    )

    formats = [payload["response_format"] for payload in transport.payloads]
    assert [item["type"] for item in formats] == ["json_object", "json_object"]
    assert all("json_schema" not in item for item in formats)
