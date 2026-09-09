"""DashScope Fun-ASR provider 单元合同（注入传输层，不出网）。

锁定的行为：短音频走 Flash 同步端点；超过阈值走异步 提交→轮询→下载；
401/403 产出凭据错误文案；异步任务失败/超时产出可读错误；transcription_url
结果解析出全文与时长。
"""

from __future__ import annotations

import json

import pytest

from app.asr import (
    AsrConfiguration,
    AsrProviderError,
    DashScopeFunAsr,
)

FLASH_THRESHOLD = 300.0


def make_config(**overrides: object) -> AsrConfiguration:
    values: dict[str, object] = {
        "base_url": "https://asr.example",
        "api_key": "test-key",
        "model": "fun-asr",
        "flash_model": "fun-asr-flash",
        "flash_threshold_sec": FLASH_THRESHOLD,
        "poll_interval_sec": 0.0,
        "poll_max_attempts": 5,
    }
    values.update(overrides)
    return AsrConfiguration(**values)  # type: ignore[arg-type]


class StubTransport:
    def __init__(self, responses: list[tuple[int, bytes]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url))
        if headers:
            assert headers.get("Authorization") == "Bearer test-key"
        return self.responses.pop(0)


def test_flash_sync_mode_for_short_audio() -> None:
    transport = StubTransport(
        [
            (
                200,
                json.dumps(
                    {
                        "output": {"text": "短音频转写结果"},
                        "usage": {"duration": 95},
                    }
                ).encode(),
            )
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    result = provider.transcribe("https://media.example/a.m4a", duration_sec=95.0)

    assert result.text == "短音频转写结果"
    assert result.duration_sec == pytest.approx(95.0)
    method, url = transport.calls[0]
    assert (method, url) == (
        "POST",
        "https://asr.example/api/v1/services/aigc/multimodal-generation/generation",
    )


def test_async_mode_submits_polls_and_downloads() -> None:
    transcription_payload = json.dumps(
        {
            "transcripts": [{"text": "长音频完整转写文本"}],
            "properties": {"original_duration_in_milliseconds": 1_250_000},
        }
    ).encode()
    transport = StubTransport(
        [
            (200, json.dumps({"output": {"task_id": "task-1"}}).encode()),
            (200, json.dumps({"output": {"task_status": "RUNNING"}}).encode()),
            (
                200,
                json.dumps(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.example/r.json",
                                }
                            ],
                        }
                    }
                ).encode(),
            ),
            (200, transcription_payload),
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    result = provider.transcribe("https://media.example/long.mp4", duration_sec=1250.0)

    assert result.text == "长音频完整转写文本"
    assert result.duration_sec == pytest.approx(1250.0)
    assert [method for method, _ in transport.calls] == ["POST", "GET", "GET", "GET"]
    assert transport.calls[1][1].endswith("/api/v1/tasks/task-1")


def test_async_failure_raises_readable_error() -> None:
    transport = StubTransport(
        [
            (200, json.dumps({"output": {"task_id": "task-2"}}).encode()),
            (
                200,
                json.dumps(
                    {
                        "output": {
                            "task_status": "FAILED",
                            "results": [{"subtask_status": "FAILED", "message": "音频无法解析"}],
                        }
                    }
                ).encode(),
            ),
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrProviderError, match="音频无法解析"):
        provider.transcribe("https://media.example/bad.mp4", duration_sec=400.0)


def test_async_poll_timeout_raises() -> None:
    responses: list[tuple[int, bytes]] = [
        (200, json.dumps({"output": {"task_id": "task-3"}}).encode())
    ] + [(200, json.dumps({"output": {"task_status": "RUNNING"}}).encode())] * 6
    provider = DashScopeFunAsr(make_config(), transport=StubTransport(responses))
    with pytest.raises(AsrProviderError, match="轮询超时"):
        provider.transcribe("https://media.example/slow.mp4", duration_sec=None)


def test_invalid_credentials_surface_config_error() -> None:
    transport = StubTransport([(401, b'{"code":"Unauthorized"}')])
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrProviderError, match="凭据无效"):
        provider.transcribe("https://media.example/a.m4a", duration_sec=10.0)


def test_missing_api_key_rejected() -> None:
    provider = DashScopeFunAsr(make_config(api_key=""), transport=StubTransport([]))
    with pytest.raises(AsrProviderError, match="未配置"):
        provider.transcribe("https://media.example/a.m4a", duration_sec=10.0)


def test_async_receipt_is_checkpointed_before_poll_and_can_resume() -> None:
    events = []
    result_output = {
        "results": [
            {"subtask_status": "SUCCEEDED", "transcription_url": "https://result.example/text"}
        ]
    }
    responses = [
        (200, json.dumps({"output": {"task_id": "durable-task"}}).encode()),
        (200, json.dumps({"output": {"task_status": "SUCCEEDED", **result_output}}).encode()),
        (200, json.dumps({"transcripts": [{"text": "已识别全文"}]}).encode()),
    ]
    transport = StubTransport(responses)
    provider = DashScopeFunAsr(make_config(), transport=transport)
    result = provider.transcribe(
        "https://media.example/source.m4a",
        duration_sec=400,
        on_submitted=lambda task_id: events.append((task_id, len(transport.calls))),
        heartbeat=lambda: None,
    )
    assert result.text == "已识别全文"
    assert events == [("durable-task", 1)]
    resumed_transport = StubTransport(responses[1:])
    resumed = DashScopeFunAsr(make_config(), transport=resumed_transport)
    assert resumed.resume("durable-task", heartbeat=lambda: None).text == result.text
    assert [method for method, _ in resumed_transport.calls] == ["GET", "GET"]


@pytest.mark.parametrize("response", [(503, b"{}"), (200, b'{"output":{}}')])
def test_async_submit_without_reliable_receipt_is_uncertain(response) -> None:
    from app.asr import AsrSubmissionUncertain

    provider = DashScopeFunAsr(make_config(), transport=StubTransport([response]))
    with pytest.raises(AsrSubmissionUncertain):
        provider.transcribe("https://media.example/long.m4a", duration_sec=400)


def test_known_receipt_result_download_failure_can_resume() -> None:
    from app.asr import AsrTaskPending

    transport = StubTransport(
        [
            (
                200,
                json.dumps(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.example/text",
                                }
                            ],
                        }
                    }
                ).encode(),
            ),
            (503, b"{}"),
        ]
    )
    provider = DashScopeFunAsr(make_config(), transport=transport)
    with pytest.raises(AsrTaskPending):
        provider.resume("existing")
    assert [method for method, _ in transport.calls] == ["GET", "GET"]
