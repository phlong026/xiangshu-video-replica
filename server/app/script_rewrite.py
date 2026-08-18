"""二创口播稿 AI 改写（默认 DeepSeek）.

读取管理员在设置页保存的 ``deepseek`` API Key，将原口播稿改写为可安全发布
的“二创口播稿”。除 API Key 外的全部参数（base_url、模型、温度、上限）都在
服务端固定默认值，界面无需暴露。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.auth import CurrentUser
from app.settings import SettingsRepository, SettingsUnavailableError

logger = logging.getLogger(__name__)

# DeepSeek 官方 OpenAI 兼容端点；config.base_url 可覆盖（例如代理/私有网关）。
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_TIMEOUT_SECONDS = 120
DEEPSEEK_MAX_OUTPUT_TOKENS = 2048

SCRIPT_REWRITE_SYSTEM_PROMPT = (
    "你是一名短视频口播稿二创作者。把你拿到的口播稿改写成一篇全新的二创口播稿，要求：\n"
    "1. 保留原文的核心信息点和节奏（镜头数量、信息密度、总字数与原文接近，误差不超过 20%）；\n"
    "2. 换一种表达方式和叙述角度重写，禁止逐句复述，避免与原文连续 8 字以上相同；\n"
    "3. 开头 3 秒必须有新的钩子（提问、反常识、利益点任选其一）；\n"
    "4. 口语化、短句为主，适合真人出镜口播；\n"
    "5. 使用与原文相同的语言（原文是中文就输出中文，是英文就输出英文）；\n"
    "6. 只输出改写后的口播稿正文，不要任何解释、标题、序号或前后缀。"
)


class ScriptRewriteRequest(BaseModel):
    """``POST /script-rewrite`` 请求体：待改写的原口播稿全文。"""

    text: str = Field(min_length=1, max_length=20000)


class ScriptRewriteResult(BaseModel):
    """改写结果：新口播稿全文与实际使用的服务标识。"""

    rewritten_text: str
    provider: str
    model: str


def rewrite_script_with_deepseek(
    conn: sqlite3.Connection,
    *,
    actor: CurrentUser,
    source_text: str,
) -> ScriptRewriteResult:
    """把 ``source_text`` 改写为二创口播稿并返回结果字典。"""
    text = source_text.strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SCRIPT_REWRITE_TEXT_REQUIRED",
                "message": "口播稿内容为空，无法改写。",
            },
        )

    try:
        config = SettingsRepository(conn).load_provider_config("deepseek")
    except SettingsUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DEEPSEEK_SETTINGS_UNAVAILABLE",
                "message": "本地配置暂不可用，请稍后重试。",
            },
        ) from exc

    api_key = config.get("api_key", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "DEEPSEEK_NOT_CONFIGURED",
                "message": (
                    "尚未配置 AI 改写服务。请管理员在"
                    + "「设置 → AI 改写」中保存 DeepSeek API Key。"
                ),
            },
        )

    base_url = (config.get("base_url") or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")
    model = config.get("model") or DEEPSEEK_DEFAULT_MODEL

    rewritten = _request_deepseek(
        base_url=base_url,
        api_key=api_key,
        model=model,
        source_text=text,
    )
    return ScriptRewriteResult(
        rewritten_text=rewritten,
        provider="deepseek",
        model=model,
    )


def _request_deepseek(
    *,
    base_url: str,
    api_key: str,
    model: str,
    source_text: str,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SCRIPT_REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": f"请改写以下口播稿：\n\n{source_text}"},
            ],
            "stream": False,
            "temperature": 1.3,
            "max_tokens": DEEPSEEK_MAX_OUTPUT_TOKENS,
        }
    ).encode("utf-8")
    request = Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        logger.warning("DeepSeek rewrite failed with HTTP status %s", exc.code)
        message = (
            "AI 改写服务 API Key 无效或无权限，请检查设置。"
            if exc.code in (401, 403)
            else "AI 改写服务返回错误，请稍后重试。"
        )
        raise HTTPException(
            status_code=502,
            detail={"code": "DEEPSEEK_REQUEST_FAILED", "message": message},
        ) from exc
    except (TimeoutError, URLError, OSError) as exc:
        logger.warning("DeepSeek rewrite failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=504,
            detail={
                "code": "DEEPSEEK_NETWORK_FAILED",
                "message": "连接 AI 改写服务失败，请检查网络后重试。",
            },
        ) from exc
    except (ValueError, KeyError) as exc:
        logger.warning("DeepSeek rewrite returned an unreadable payload")
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DEEPSEEK_RESPONSE_INVALID",
                "message": "AI 改写服务返回内容异常，请重试。",
            },
        ) from exc

    try:
        content = str(body["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DEEPSEEK_RESPONSE_INVALID",
                "message": "AI 改写服务返回内容异常，请重试。",
            },
        ) from exc
    if not content:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "DEEPSEEK_RESPONSE_EMPTY",
                "message": "AI 改写结果为空，请重试。",
            },
        )
    return content
