from __future__ import annotations

import json

import pytest

from app.account_diagnosis_llm import (
    _validated_narrative,
    build_llm_narrative,
    load_narrative_llm_config,
    narrative_llm_disabled,
)

_VALID_CONTENT = json.dumps(
    {
        "summary": "综合 72 分（正常）。最突出的短板是互动率低于腰部基准。",
        "strengths": [{"dimension": "内容质量", "reason": "爆款率 20% 高于基准"}],
        "weaknesses": [{"dimension": "互动健康度", "reason": "粉丝互动率 2.1% 低于 4%"}],
        "actions": [
            {
                "title": "粉丝互动率",
                "evidence": "2.10%（腰部基准 6.0/4.0/2.0%）",
                "suggestion": "结尾抛一个具体问题，把看完转成说两句。",
            }
        ],
        "generatedBy": "llm",
    },
    ensure_ascii=False,
)


def _chat(content: object) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": content}}]},
        ensure_ascii=False,
    ).encode("utf-8")


def _report() -> dict:
    return {
        "score": 72,
        "rating": "正常",
        "tier_label": "腰部",
        "data_level": "L1",
        "sample_size": 8,
        "metrics": {},
        "dimensions": [
            {
                "label": "互动健康度",
                "score": 18,
                "effective_full": 30,
                "comment": "粉丝互动率",
                "sub_scores": [
                    {
                        "label": "粉丝互动率",
                        "score": 4,
                        "full": 10,
                        "counted": True,
                        "detail": "2.10%",
                    }
                ],
            }
        ],
        "risks": [{"kind": "decline", "label": "衰退", "level": "medium", "evidence": "-62%"}],
    }


def test_build_llm_narrative_validates_and_normalizes() -> None:
    def _transport(url: str, payload: bytes) -> tuple[int, bytes]:
        sent = json.loads(payload)
        assert sent["response_format"] == {"type": "json_object"}
        assert sent["model"] == "deepseek-flash"
        assert sent["thinking"] == {"type": "disabled"}
        assert "互动健康度" in sent["messages"][-1]["content"]
        return 200, _chat(_VALID_CONTENT)

    narrative = build_llm_narrative(
        base_url="https://api.deepseek.com",
        api_key="key",
        model="deepseek-flash",
        report=_report(),
        post_json=_transport,
    )
    assert narrative is not None
    assert narrative["generatedBy"] == "llm"
    assert narrative["summary"].startswith("综合 72 分")
    assert narrative["actions"][0]["suggestion"].startswith("结尾抛一个具体问题")


def test_build_llm_narrative_rejects_malformed_output() -> None:
    cases = [
        None,
        "不是 JSON",
        json.dumps({"summary": ""}),
        json.dumps({"summary": "只有总结没有行动"}),
        json.dumps({"summary": "x", "actions": [{"title": 1}]}),
        _chat({"unexpected": True}).decode("utf-8"),
    ]
    for content in cases:
        narrative = build_llm_narrative(
            base_url="https://api.deepseek.com",
            api_key="key",
            model="deepseek-chat",
            report=_report(),
            post_json=lambda _url, _payload, content=content: (200, _chat(content)),
        )
        assert narrative is None, content


def test_build_llm_narrative_http_error_falls_back_to_none() -> None:
    narrative = build_llm_narrative(
        base_url="https://api.deepseek.com",
        api_key="key",
        model="deepseek-chat",
        report=_report(),
        post_json=lambda _url, _payload: (503, b"busy"),
    )
    assert narrative is None


def test_validated_narrative_tolerates_missing_optional_sections() -> None:
    payload = json.loads(
        json.dumps(
            {
                "summary": "总结",
                "actions": [{"title": "t", "suggestion": "s"}],
            },
            ensure_ascii=False,
        )
    )
    narrative = _validated_narrative(payload)
    assert narrative is not None
    assert narrative["strengths"] == []
    assert narrative["weaknesses"] == []
    assert narrative["actions"][0]["evidence"] == ""


def test_narrative_llm_switch_defaults_to_enabled(monkeypatch) -> None:
    monkeypatch.delenv("ACCOUNT_DIAGNOSIS_LLM_DISABLED", raising=False)
    assert narrative_llm_disabled() is False
    monkeypatch.setenv("ACCOUNT_DIAGNOSIS_LLM_DISABLED", "1")
    assert narrative_llm_disabled() is True


def test_load_narrative_llm_config_handles_missing_settings(monkeypatch) -> None:
    class _BrokenRepo:
        def __init__(self, _conn) -> None:
            pass

        def load_provider_config(self, _name: str) -> dict:
            raise RuntimeError("settings unavailable")

    import app.account_diagnosis_llm as module

    monkeypatch.setattr(module, "SettingsRepository", _BrokenRepo)
    assert load_narrative_llm_config(object()) is None


def test_load_narrative_llm_config_requires_api_key(monkeypatch) -> None:
    class _Repo:
        def __init__(self, _conn) -> None:
            pass

        def load_provider_config(self, _name: str) -> dict:
            return {"api_key": "  "}

    import app.account_diagnosis_llm as module

    monkeypatch.setattr(module, "SettingsRepository", _Repo)
    assert load_narrative_llm_config(object()) is None


@pytest.mark.parametrize(
    ("configured_model", "expected"),
    [
        ("", "deepseek-flash"),
        ("deepseek-chat", "deepseek-flash"),
        ("deepseek-v4-flash", "deepseek-flash"),
        ("deepseek-flash", "deepseek-flash"),
        ("custom-text-model", "custom-text-model"),
    ],
)
def test_configured_deepseek_model_is_shared(monkeypatch, configured_model, expected) -> None:
    from app import account_diagnosis_llm, script_rewrite

    class _Repo:
        def __init__(self, _conn) -> None:
            pass

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "deepseek"
            return {"api_key": "test-key", "model": configured_model}

    monkeypatch.setattr(account_diagnosis_llm, "SettingsRepository", _Repo)
    monkeypatch.setattr(script_rewrite, "SettingsRepository", _Repo)
    expected_config = ("https://api.deepseek.com", "test-key", expected)
    assert account_diagnosis_llm.load_narrative_llm_config(object()) == expected_config
    assert script_rewrite.load_script_rewrite_configuration(object()) == expected_config
