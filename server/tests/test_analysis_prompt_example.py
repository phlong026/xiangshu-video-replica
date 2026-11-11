"""拆解提示词的输出示例必须原样通过严格出参校验（方案 T8）。

示例是 few-shot 锚点：示例自身若过不了 ProviderShotCard 合同，模型模仿示例
产出的结果也过不了，拆解会在校验层大面积失败。示例与占位符的共存同样要
守住——占位符替换逻辑不得被示例内的花括号破坏。
"""

from __future__ import annotations

import json
import re

from app.analysis import parse_analysis_response
from app.h3_prompts import RULES

# 示例自带的虚构媒体事实：示例时间轴必须恰好覆盖这个时长。
EXAMPLE_DURATION_SECONDS = 12.0


def _instruction_text() -> str:
    return (RULES / "analysis.txt").read_text(encoding="utf-8")


def _example_payload() -> dict:
    match = re.search(r"示例开始\n(.*?)\n示例结束", _instruction_text(), re.S)
    assert match is not None, "analysis.txt 缺少「九、输出示例」节"
    payload = json.loads(match.group(1))
    assert isinstance(payload, dict)
    return payload


def test_example_passes_the_strict_provider_contract() -> None:
    payload = _example_payload()
    # 与 analyze_video 的拆包顺序一致：严格解析器吃信封内的 analysis 对象。
    # 不抛 ValidationError 即通过：ProviderShotCard 的 motion/segment_kind/
    # boundary_reason/person_count 严格合同与时间轴合同全部生效。
    parse_analysis_response(
        json.dumps(payload["analysis"]), duration_seconds=EXAMPLE_DURATION_SECONDS
    )


def test_example_timeline_covers_example_duration() -> None:
    shots = _example_payload()["analysis"]["shots"]
    assert shots[0]["start_time"] == 0
    assert shots[-1]["end_time"] == EXAMPLE_DURATION_SECONDS
    kinds = [shot["segment_kind"] for shot in shots]
    assert "SHOT_CUT" in kinds and "ACTION_BEAT" in kinds


def test_example_script_matches_segment_speech() -> None:
    analysis = _example_payload()["analysis"]
    joined = "".join(str(shot["spoken_text"]) for shot in analysis["shots"])

    def normalize(text: str) -> str:
        return re.sub(r"\s", "", text)

    assert normalize(joined) == normalize(str(analysis["original_script"]))


def test_media_placeholder_keys_survive_next_to_the_example() -> None:
    instruction = _instruction_text()
    for key in (
        "{MEDIA_INFO_JSON}",
        "{GENERATION_CONTEXT_JSON}",
        "{SCENE_BOUNDARY_GUIDANCE_JSON}",
    ):
        assert key in instruction


def test_example_warns_against_copying_values() -> None:
    instruction = _instruction_text()
    assert "数值一律不得照抄" in instruction
    assert "真实 duration_seconds" in instruction
