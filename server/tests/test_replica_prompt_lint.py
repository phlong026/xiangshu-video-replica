"""长视频出参 lint（方案 T10）：禁词硬拒、时长/画幅/长度软警告.

lint 只有一类硬拒绝（禁词）；要素缺失一律走 warnings 随 payload 落库，
不丢整次付费调用。评审变更集 #7/#11 的正反例在此锁定。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.analysis import AnalysisProviderFailed, parse_replica_prompt_payload

MEDIA: dict[str, Any] = {"duration_seconds": 179.8, "aspect_ratio": "9:16", "fps": 24.0}


def _payload(prompt: str) -> str:
    return json.dumps(
        {
            "schema_version": "replica-prompt.v1",
            "summary": {
                key: "x"
                for key in ("theme", "visual_style", "pace", "camera_language", "color_tone")
            },
            "spoken_script": [],
            "replica_prompt": prompt,
        },
        ensure_ascii=False,
    )


def test_clean_prompt_with_full_facts_has_no_warnings() -> None:
    out = parse_replica_prompt_payload(
        _payload("庭院实景讲解，全长 179.8 秒，画幅 9:16，镜头以固定机位为主。"),
        media_info=MEDIA,
    )

    assert out["warnings"] == []


def test_rounded_up_duration_wording_is_accepted() -> None:
    """「约 180 秒」是对 179.8s 的自然四舍五入，不应误报。"""
    out = parse_replica_prompt_payload(
        _payload("全长约 180 秒的庭院讲解，画幅 9:16。"), media_info=MEDIA
    )

    assert out["warnings"] == []


def test_bare_fps_number_is_not_mistaken_for_duration() -> None:
    """「30fps」里的 30 不是时长：必须报 REPLICA_DURATION_MISSING（评审 #7）。"""
    out = parse_replica_prompt_payload(
        _payload("30fps 的动画短片讲解，画幅 9:16。"), media_info={**MEDIA, "duration_seconds": 30}
    )

    assert [w["code"] for w in out["warnings"]] == ["REPLICA_DURATION_MISSING"]


def test_missing_aspect_ratio_is_a_soft_warning() -> None:
    out = parse_replica_prompt_payload(_payload("全长 179 秒的庭院讲解。"), media_info=MEDIA)

    assert [w["code"] for w in out["warnings"]] == ["REPLICA_ASPECT_RATIO_MISSING"]


def test_oversized_prompt_is_a_soft_warning() -> None:
    out = parse_replica_prompt_payload(_payload("庭院讲解。" + "长" * 9000), media_info=MEDIA)

    assert "REPLICA_PROMPT_TOO_LONG" in [w["code"] for w in out["warnings"]]


@pytest.mark.parametrize("word", ["复刻", "原片", "源视频"])
def test_forbidden_words_are_hard_rejected(word: str) -> None:
    with pytest.raises(AnalysisProviderFailed) as caught:
        parse_replica_prompt_payload(_payload(f"照此{word}这条视频。"), media_info=MEDIA)

    assert caught.value.retryable is False
    assert (caught.value.upstream_diagnostic or {})["reason"] == "replica_prompt_forbidden_word"


def test_historical_payload_without_warnings_key_still_parses() -> None:
    """历史版本没有 warnings 键：解析兼容，warnings 补为空列表。"""
    out = parse_replica_prompt_payload(
        _payload("全长 179 秒，画幅 9:16 的讲解。"), media_info=MEDIA
    )

    assert out["warnings"] == []
    assert out["schema_version"] == "replica-prompt.v1"
