"""storyboard 模式（分镜表拆解）解析器与入队的回归.

EB1CA38A/F82C66E9 之后的延续：LLM 结构化输出在长视频链路同样不稳定，
解析取舍与 parse_replica_prompt_payload 同源——宽容恢复、禁词硬拒、
时间轴一致性只软警告。
"""

from __future__ import annotations

import json

import pytest

from app.analysis import (
    AnalysisProviderFailed,
    default_replica_storyboard_payload,
    parse_replica_storyboard_payload,
)


def _valid_payload() -> dict:
    return {
        "schema_version": "long-analysis.v1",
        "summary": {
            "theme": "乡村讲解",
            "visual_style": "实拍",
            "pace": "中速",
            "camera_language": "固定机位",
            "color_tone": "自然",
        },
        "spoken_script": [],
        "replica_prompt": "一段可直接投喂生成模型的完整提示词。",
        "shots": [
            {
                "shot_id": "s1",
                "start_time": 0,
                "end_time": 45.5,
                "shot_type": "中景",
                "camera_motion": "固定机位",
                "subject": "讲解者",
                "person_count": 1,
                "action": "面向镜头讲解",
                "scene": "乡村小巷",
                "spoken_text": "",
                "transition": "硬切",
            },
            {
                "shot_id": "s2",
                "start_time": 45.5,
                "end_time": 60,
                "shot_type": "近景",
                "camera_motion": "手持",
                "subject": "讲解者",
                "person_count": 1,
                "action": "侧身示意",
                "scene": "庭院",
                "spoken_text": "",
                "transition": "无切换",
            },
        ],
    }


def test_valid_storyboard_parses_without_warnings() -> None:
    payload = parse_replica_storyboard_payload(
        json.dumps(_valid_payload(), ensure_ascii=False),
        media_info={"duration_seconds": 60},
    )
    assert len(payload["shots"]) == 2
    assert payload["warnings"] == []
    assert payload["replica_prompt"] == "一段可直接投喂生成模型的完整提示词。"


def test_raw_newline_injection_recovers() -> None:
    raw = json.dumps(_valid_payload(), ensure_ascii=False)
    injected = raw.replace('"shot_id"', '"\nshot_id\n"').replace('"乡村小巷"', '"乡村小\n巷"')
    payload = parse_replica_storyboard_payload(injected)
    assert len(payload["shots"]) == 2


def test_missing_shots_is_rejected() -> None:
    payload = _valid_payload()
    del payload["shots"]
    with pytest.raises(AnalysisProviderFailed, match="没有产出分镜表"):
        parse_replica_storyboard_payload(json.dumps(payload, ensure_ascii=False))


def test_forbidden_word_is_hard_rejected() -> None:
    payload = _valid_payload()
    payload["replica_prompt"] = "复刻原片的一段提示词。"
    with pytest.raises(AnalysisProviderFailed, match="内部任务表述"):
        parse_replica_storyboard_payload(json.dumps(payload, ensure_ascii=False))


def test_duration_mismatch_is_warning_not_rejection() -> None:
    payload = _valid_payload()
    payload["shots"][-1]["end_time"] = 31
    parsed = parse_replica_storyboard_payload(
        json.dumps(payload, ensure_ascii=False),
        media_info={"duration_seconds": 60},
    )
    codes = {warning["code"] for warning in parsed["warnings"]}
    assert "STORYBOARD_DURATION_MISMATCH" in codes


def test_default_payload_shape_matches_parser() -> None:
    raw = json.dumps(default_replica_storyboard_payload(120), ensure_ascii=True, sort_keys=True)
    parsed = parse_replica_storyboard_payload(raw, media_info={"duration_seconds": 120})
    assert len(parsed["shots"]) >= 2
    assert parsed["warnings"] == []


@pytest.mark.parametrize(
    "shots",
    [
        [],
        [None],
        [7],
        ["镜头内容"],
        [False],
        [[]],
        [{}],
        [{"unknown": "无已知镜头字段"}],
        [{"shot_id": "s1", "person_count": 1}],
        [{"shot_id": "s1"}],
        [{"person_count": 1}],
        [{"start_time": 0}],
        [{"end_time": 60}],
        [{"start_time": -1, "end_time": 1}],
        [{"start_time": 2, "end_time": 1}],
        [{"start_time": 0, "end_time": 0}],
        [{"subject": "  ", "spoken_text": ""}],
        [{"subject": {"text": "错误类型"}, "scene": ["错误类型"]}],
    ],
)
def test_storyboard_without_usable_shots_is_rejected(shots: list[object]) -> None:
    payload = {**_valid_payload(), "shots": shots}
    with pytest.raises(AnalysisProviderFailed, match="没有产出分镜表") as error:
        parse_replica_storyboard_payload(json.dumps(payload, ensure_ascii=False))
    assert error.value.retryable is False


def test_storyboard_filters_bad_rows_and_normalizes_only_known_fields() -> None:
    valid = _valid_payload()["shots"][1]
    payload = {
        **_valid_payload(),
        "shots": [
            None,
            7,
            {},
            {
                " \nshot_id\t": "s1",
                " subject ": "讲解者",
                "start_time": 0,
                " end_time\n": 45.5,
                "person_count": 0,
                "extra": {"internal": "不应透传"},
            },
            valid,
        ],
    }
    parsed = parse_replica_storyboard_payload(
        json.dumps(payload, ensure_ascii=False), media_info={"duration_seconds": 60}
    )
    assert parsed["shots"] == [
        {
            "shot_id": "s1",
            "subject": "讲解者",
            "start_time": 0,
            "end_time": 45.5,
            "person_count": 0,
        },
        valid,
    ]
    assert parsed["warnings"] == []


@pytest.mark.parametrize("bad_text", [{"text": "错误类型"}, ["错误类型"]])
def test_storyboard_drops_non_string_text_fields(bad_text: object) -> None:
    payload = _valid_payload()
    payload["shots"][0].update({"shot_id": bad_text, "action": bad_text, "spoken_text": bad_text})
    parsed = parse_replica_storyboard_payload(json.dumps(payload, ensure_ascii=False))
    shot = parsed["shots"][0]
    assert "shot_id" not in shot
    assert "action" not in shot
    assert "spoken_text" not in shot
    assert shot["subject"] == "讲解者"


@pytest.mark.parametrize(
    "invalid_number",
    [float("nan"), float("inf"), -float("inf"), True, False, "3", {}, [], 10**400],
)
def test_storyboard_omits_invalid_numbers_and_warns_about_missing_timeline(
    invalid_number: object,
) -> None:
    payload = {
        **_valid_payload(),
        "shots": [
            {
                "subject": "讲解者",
                "start_time": invalid_number,
                "end_time": invalid_number,
                "person_count": invalid_number,
            }
        ],
    }
    parsed = parse_replica_storyboard_payload(
        json.dumps(payload, ensure_ascii=False), media_info={"duration_seconds": 60}
    )
    assert parsed["shots"] == [{"subject": "讲解者"}]
    assert [warning["code"] for warning in parsed["warnings"]] == ["STORYBOARD_DURATION_MISMATCH"]
    json.dumps(parsed, allow_nan=False)


@pytest.mark.parametrize("person_count", [-1, 1.5])
def test_storyboard_omits_negative_or_fractional_person_count(person_count: float) -> None:
    payload = {**_valid_payload(), "shots": [{"subject": "讲解者", "person_count": person_count}]}
    parsed = parse_replica_storyboard_payload(json.dumps(payload, ensure_ascii=False))
    assert parsed["shots"] == [{"subject": "讲解者"}]


@pytest.mark.parametrize(
    "shot",
    [
        {"subject": "讲解者"},
        {"start_time": 0, "end_time": 3.5},
        {"composition": "人物居中", "spoken_text": "", "person_count": 1},
    ],
)
def test_storyboard_keeps_partial_shots_without_inventing_missing_fields(shot: dict) -> None:
    payload = {**_valid_payload(), "shots": [shot]}
    parsed = parse_replica_storyboard_payload(json.dumps(payload, ensure_ascii=False))
    assert parsed["shots"] == [shot]
