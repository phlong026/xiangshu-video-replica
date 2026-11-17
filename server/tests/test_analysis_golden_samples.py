"""黄金集回放：生产真实响应样本必须能被当前解析链恢复（换模型/网关行为
变化前后的回归锚点）。

样本来源（external_call_logs，gemini-3.8-flash，2026-11-17 导出）：

- broken_eb1ca38a_envelope.json：EB1CA38A 报障任务（20.689s 源片）——
  内层 content 带裸换行注入，修复前必然解析失败；
- good_recent_envelope.json：同期 SUCCEEDED 调用（12.144s 源片）——
  同样带裸换行注入。两条样本证明 3.8 的注入是常态性行为而非偶发。

任何一条样本解析失败的修复回归，都说明解析链与当前网关/模型行为脱钩。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.analysis import FakeGemini, analyze_video
from app.llm_json import parse_llm_json

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden_analysis"
GOLDEN_FIXTURES = [
    "broken_eb1ca38a_envelope.json",
    "good_recent_envelope.json",
]


def _load_content(fixture_name: str) -> tuple[str, float]:
    envelope = json.loads((GOLDEN_DIR / fixture_name).read_text(encoding="utf-8"))
    content = envelope["choices"][0]["message"]["content"]
    raw_probe = json.loads(content.replace("\r", "").replace("\n", ""))
    duration = float(raw_probe["analysis"]["duration_seconds"])
    return content, duration


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURES)
def test_golden_content_fails_raw_json_loads(fixture_name: str) -> None:
    # 锚点：注入在场时原样 json.loads 必失败——若某天原样能过，说明网关/
    # 模型行为已修复，宽容链退化为纯保险（本锚点用于感知行为变化）。
    content, _ = _load_content(fixture_name)
    with pytest.raises(json.JSONDecodeError):
        json.loads(content)


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURES)
def test_golden_content_recovers_through_lenient_chain(fixture_name: str) -> None:
    content, _ = _load_content(fixture_name)
    payload, strategy = parse_llm_json(content)
    assert payload.get("schema_version") == "analysis-h3.v1"
    assert strategy != "raw"
    analysis = payload.get("analysis")
    assert isinstance(analysis, dict) and analysis.get("shots")


@pytest.mark.parametrize("fixture_name", GOLDEN_FIXTURES)
def test_golden_content_survives_analyze_video(fixture_name: str) -> None:
    content, duration = _load_content(fixture_name)
    result = analyze_video(
        video_uri="fake",
        video_duration_seconds=duration,
        provider=FakeGemini(analysis_json=content),
    )
    assert result.analysis.shots
    assert result.analysis.duration_seconds == pytest.approx(duration)
    assert result.analysis.resolution == "1080x1920"
