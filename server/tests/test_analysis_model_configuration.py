"""视频拆解的模型名要能在设置里换，但 base_url 必须继续钉死。

上游把 preview 模型下线时，整条拆解链路会以 HTTP 400 全量失败，而模型名此前
硬编码在 ``APILIO_ANALYSIS_MODEL``，换一个名字就得改代码重新发版。base_url 不
一样：它决定配置里的 bearer token 发给谁，放开等于把凭据交给任意地址，所以
即便配置里存了 base_url 也必须忽略。
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app import analysis_routes
from app.analysis import APILIO_ANALYSIS_MODEL, APILIO_DEFAULT_BASE_URL, ApilioGemini


def _provider_for(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> ApilioGemini:
    repository = Mock()
    repository.load_provider_config.side_effect = lambda provider: (
        config if provider == "apilio" else {}
    )
    monkeypatch.setattr(analysis_routes, "SettingsRepository", lambda conn: repository)
    provider = analysis_routes.get_video_analysis_provider(Mock())
    assert isinstance(provider, ApilioGemini)
    return provider


def test_saved_analysis_model_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider_for(
        monkeypatch, {"analysis_api_key": "k", "analysis_model": "gemini-config-override-probe"}
    )

    assert provider.model == "gemini-config-override-probe"


def test_missing_analysis_model_falls_back_to_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider_for(monkeypatch, {"analysis_api_key": "k"})

    assert provider.model == APILIO_ANALYSIS_MODEL


def test_blank_analysis_model_falls_back_instead_of_sending_an_empty_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置页清空输入框存下来是空串；空 model 会让上游必然 400。"""
    provider = _provider_for(monkeypatch, {"analysis_api_key": "k", "analysis_model": "   "})

    assert provider.model == APILIO_ANALYSIS_MODEL


def test_saved_base_url_is_still_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """放开 model 不等于放开 endpoint —— 凭据只能发往固定源站。"""
    provider = _provider_for(
        monkeypatch,
        {
            "analysis_api_key": "k",
            "analysis_model": "gemini-config-override-probe",
            "base_url": "https://attacker.example.com",
        },
    )

    assert provider.base_url == APILIO_DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# 按链路路由（决策 D9，Phase 0 分段实测驱动）：分镜主链路 3.8-flash（分段
# 精准），长视频一段式 4-carbon（快且零断连）。出厂值在此钉死，防止无意翻转。
# ---------------------------------------------------------------------------


def test_per_lane_factory_defaults_are_pinned() -> None:
    from app.analysis import (
        APILIO_ANALYSIS_FALLBACK_MODELS,
        APILIO_REPLICA_PROMPT_FALLBACK_MODELS,
        APILIO_REPLICA_PROMPT_MODEL,
    )

    assert APILIO_ANALYSIS_MODEL == "gemini-3.8-flash"
    assert APILIO_ANALYSIS_FALLBACK_MODELS == ("gemini-4-carbon",)
    assert APILIO_REPLICA_PROMPT_MODEL == "gemini-4-carbon"
    assert APILIO_REPLICA_PROMPT_FALLBACK_MODELS == ("gemini-3.8-flash",)


def _long_provider_for(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> ApilioGemini:
    from app.replica_prompt import build_long_video_provider

    repository = Mock()
    repository.load_provider_config.side_effect = lambda provider: (
        config if provider == "apilio" else {}
    )
    monkeypatch.setattr(analysis_routes, "SettingsRepository", lambda conn: repository)
    provider = build_long_video_provider(Mock())
    assert isinstance(provider, ApilioGemini)
    return provider


def test_long_video_lane_defaults_to_its_own_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长视频链路出厂 4-carbon：3.8-flash 在 180s 素材上两次网关断连。"""
    provider = _long_provider_for(monkeypatch, {"analysis_api_key": "k"})

    assert provider.model == "gemini-4-carbon"
    assert provider.fallback_models == ("gemini-3.8-flash",)
    assert provider.video_call_budget is not None


def test_long_video_lane_ignores_main_lane_model_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主链路设置 analysis_model 不得串到长视频链路：两条链路各自独立。"""
    provider = _long_provider_for(
        monkeypatch,
        {
            "analysis_api_key": "k",
            "analysis_model": "gemini-main-lane-probe",
            "analysis_model_fallbacks": "gemini-main-fallback-probe",
        },
    )

    assert provider.model == "gemini-4-carbon"
    assert provider.fallback_models == ("gemini-3.8-flash",)


def test_long_video_lane_honors_its_own_setting_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _long_provider_for(
        monkeypatch,
        {
            "analysis_api_key": "k",
            "replica_prompt_model": "gemini-replica-override-probe",
            "replica_prompt_model_fallbacks": "gemini-replica-fallback-probe",
        },
    )

    assert provider.model == "gemini-replica-override-probe"
    assert provider.fallback_models == ("gemini-replica-fallback-probe",)


def test_analysis_lane_ignores_replica_lane_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """长视频键不得串到分镜主链路：反向同样独立。"""
    provider = _provider_for(
        monkeypatch,
        {
            "analysis_api_key": "k",
            "replica_prompt_model": "gemini-replica-override-probe",
        },
    )

    assert provider.model == APILIO_ANALYSIS_MODEL
