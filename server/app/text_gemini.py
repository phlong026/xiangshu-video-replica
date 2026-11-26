"""Gemini 的文本调用使用现有模型网关。"""

from app.analysis import (
    AnalysisProviderFailed,
    ApilioGemini,
    UrllibApilioChatTransport,
    _should_try_fallback,
)
from app.db_portable import BusinessConnection
from app.settings import SettingsRepository, SettingsUnavailableError

GEMINI_TEXT_MODEL = "gemini-4-carbon"


def load_gemini_text_provider(
    conn: BusinessConnection, *, timeout_seconds: float = 30
) -> ApilioGemini | None:
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError:
        return None
    key = config.get("analysis_api_key") or config.get("api_key")
    if not key:
        return None
    return ApilioGemini(
        api_key=key,
        model=GEMINI_TEXT_MODEL,
        fallback_models=(),
        transport=UrllibApilioChatTransport(timeout_seconds=timeout_seconds),
    )


def gemini_can_fail_over(failure: AnalysisProviderFailed) -> bool:
    # 请求结果未知时禁止重发。只在上游明确拒绝或明确不可用时切换通道。
    return failure.http_status in (401, 402, 403, 503) or _should_try_fallback(
        failure, model=GEMINI_TEXT_MODEL
    )


def complete_gemini_text(
    provider: ApilioGemini,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    json_output: bool = False,
) -> str:
    content, _ = provider._complete(
        {
            "model": provider.model,
            "messages": messages,
            "stream": False,
            "temperature": 0 if json_output else 0.3,
            "max_tokens": max_tokens,
            **({"response_format": {"type": "json_object"}} if json_output else {}),
        }
    )
    return content.strip()
