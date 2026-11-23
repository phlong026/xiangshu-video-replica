from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from app.replica_result_review import (
    replica_expectations,
    review_generated_replica,
    unavailable_replica_review,
)


def expectations() -> dict:
    return {
        "shots": [
            {
                "shot_id": "s1",
                "start_time": 0,
                "end_time": 2,
                "segment_kind": "ACTION_BEAT",
                "action": "人物走向门口并做手势",
                "motion": {"subject_motion_state": "WALKING+GESTURING_ONLY"},
                "camera_motion": "固定机位",
                "scene": "源场景庭院",
            },
            {
                "shot_id": "s2",
                "start_time": 2,
                "end_time": 4,
                "segment_kind": "SHOT_CUT",
                "action": "人物原地静止",
                "motion": {"subject_motion_state": "STATIC"},
                "camera_motion": "固定机位",
                "scene": "源场景石桌",
            },
        ],
        "source_duration_seconds": 4,
        "output_duration_seconds": 8,
        "opening_action": "",
        "replace_scene": False,
        "script_text": "看看院子",
        "prompt_text": "最终确认：先走向门口，再原地介绍庭院；台词：看看院子。",
    }


def observation(start: float, end: float) -> dict:
    return {
        "status": "MATCH",
        "observed": "已对照连续画面观察到对应过程",
        "timestamps": [start, end],
    }


def result_payload() -> dict:
    return {
        "shots": [
            {
                "shot_id": shot_id,
                **{key: observation(start, end) for key in ("action", "camera", "timing")},
            }
            for shot_id, start, end in [("s1", 0.1, 3.9), ("s2", 4.1, 7.9)]
        ],
        "speech": {**observation(0.1, 7.9), "observed": "实际台词为看看院子，没有额外人声"},
    }


class Provider:
    def __init__(self, payload: object = None, *, error: Exception | None = None):
        self.payload = result_payload() if payload is None else payload
        self.error = error
        self.calls = []

    def review_generated_replica(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=json.dumps(self.payload))


def review(provider: object, expected: dict | None = None, *, prompt_text: str | None = None):
    expected = expectations() if expected is None else expected
    return review_generated_replica(
        provider=provider,
        video_url="https://example.test/result.mp4",
        expectations=expected,
        prompt_text=prompt_text or expected.get("prompt_text", "最终稿"),
        output_duration_seconds=8,
    )


def context(provider: Provider) -> dict:
    return json.loads(provider.calls[0]["instructions"].split("REVIEW_CONTEXT_JSON=", 1)[1])


def test_only_new_compiled_snapshots_opt_into_review_and_bad_data_is_not_exempt():
    assert replica_expectations({"prompt_snapshot_json": '{"final_composition":true}'}) is None
    assert replica_expectations({"prompt_snapshot_json": "broken-json"}) == {}
    assert replica_expectations({"prompt_snapshot_json": '{"replica_expectations":null}'}) == {}
    assert replica_expectations({"prompt_snapshot_json": '{"replica_expectations":[]}'}) == {}
    expected = expectations()
    assert (
        replica_expectations(
            {"prompt_snapshot_json": json.dumps({"replica_expectations": expected})}
        )
        == expected
    )


def test_complete_observed_matches_pass_once_with_scaled_segment_times():
    provider = Provider()
    expected = expectations()
    original = copy.deepcopy(expected)
    result = review(provider, expected)
    assert result.quality_status == "REPLICA_REVIEW_PASSED"
    assert result.issue_codes == []
    assert len(provider.calls) == 1
    assert provider.calls[0]["video_uri"] == "https://example.test/result.mp4"
    assert provider.calls[0]["duration_seconds"] == 8
    segments = context(provider)["segments"]
    assert [(s["start_time"], s["end_time"]) for s in segments] == [(0, 4), (4, 8)]
    assert segments[0]["motion"]["subject_motion_state"] == "WALKING+GESTURING_ONLY"
    assert expected == original


@pytest.mark.parametrize("dimension", ["action", "camera", "timing"])
def test_observed_mismatches_identify_the_shot(dimension: str):
    payload = result_payload()
    payload["shots"][0][dimension]["status"] = "MISMATCH"
    result = review(Provider(payload))
    assert result.quality_status == "REPLICA_REVIEW_REQUIRED"
    assert result.issue_codes == [f"REPLICA_{dimension.upper()}_MISMATCH:s1"]


def test_extra_speech_in_a_silent_script_is_a_mismatch():
    expected = expectations()
    expected["script_text"] = ""
    expected["prompt_text"] = "无口播，不添加台词或人声旁白。"
    payload = result_payload()
    payload["speech"].update(status="MISMATCH", observed="实际出现了未经要求的人声台词")
    provider = Provider(payload)
    result = review(provider, expected)
    assert "REPLICA_SPEECH_MISMATCH" in result.issue_codes
    assert context(provider)["confirmed_script"] == ""
    assert "无台词" in provider.calls[0]["instructions"]


@pytest.mark.parametrize(
    "bad_kind",
    [
        "unknown",
        "invalid_status_type",
        "unobserved",
        "empty_observation",
        "missing_dimension",
        "missing_shot",
        "duplicate_shot",
        "foreign_shot",
        "outside_segment",
        "outside_video",
        "reversed_times",
        "one_time",
        "boolean_time",
        "nan_time",
        "missing_speech",
        "not_an_object",
    ],
)
def test_unobserved_or_incomplete_evidence_never_passes(bad_kind: str):
    payload = result_payload()
    action = payload["shots"][0]["action"]
    if bad_kind == "unknown":
        action["status"] = "UNKNOWN"
    elif bad_kind == "invalid_status_type":
        action["status"] = ["MATCH"]
    elif bad_kind == "unobserved":
        action["observed"] = "无法观察人物是否移动"
    elif bad_kind == "empty_observation":
        action["observed"] = "  "
    elif bad_kind == "missing_dimension":
        del payload["shots"][0]["camera"]
    elif bad_kind == "missing_shot":
        payload["shots"].pop()
    elif bad_kind == "duplicate_shot":
        payload["shots"][1]["shot_id"] = "s1"
    elif bad_kind == "foreign_shot":
        payload["shots"][1]["shot_id"] = "s9"
    elif bad_kind == "outside_segment":
        action["timestamps"] = [0.1, 6]
    elif bad_kind == "outside_video":
        payload["speech"]["timestamps"] = [0.1, 8.1]
    elif bad_kind == "reversed_times":
        action["timestamps"] = [2, 1]
    elif bad_kind == "one_time":
        action["timestamps"] = [1]
    elif bad_kind == "boolean_time":
        action["timestamps"] = [False, 1]
    elif bad_kind == "nan_time":
        action["timestamps"] = [0.1, float("nan")]
    elif bad_kind == "missing_speech":
        del payload["speech"]
    else:
        payload = []
    result = review(Provider(payload))
    assert result.quality_status == "REPLICA_REVIEW_REQUIRED"
    assert "REPLICA_REVIEW_INCOMPLETE" in result.issue_codes


@pytest.mark.parametrize(
    "observed",
    [
        "未观看视频，但根据预期应该符合",
        "未能获取视频，按提示词推测一致",
        "没有看过视频，按描述推测一致",
        "I have not watched the video; inferred from the prompt.",
        "仅根据提示词推测人物在行走",
        "Inferred from the prompt; no actual footage was inspected.",
    ],
)
def test_claimed_match_without_watching_or_only_by_inference_is_incomplete(observed: str):
    payload = result_payload()
    payload["shots"][0]["action"]["observed"] = observed
    result = review(Provider(payload))

    assert result.quality_status == "REPLICA_REVIEW_REQUIRED"
    assert result.issue_codes == ["REPLICA_REVIEW_INCOMPLETE"]


@pytest.mark.parametrize(
    "observed",
    [
        "已查看成片：人物没有停下，连续走向门口；固定背景未改变。",
        "I watched the generated video: the person did not stand still and walked forward.",
    ],
)
def test_observed_negative_action_is_not_confused_with_not_watching(observed: str):
    payload = result_payload()
    payload["shots"][0]["action"]["observed"] = observed

    assert review(Provider(payload)).quality_status == "REPLICA_REVIEW_PASSED"


def test_first_frame_opening_override_replaces_stale_first_motion():
    expected = expectations()
    expected["opening_action"] = "从确认首帧的坐姿开始，坐着转头看向庭院"
    provider = Provider()
    review(provider, expected)
    first = context(provider)["segments"][0]
    assert first["action"] == expected["opening_action"]
    assert "motion" not in first


def test_replaced_scene_does_not_require_old_scene_objects():
    expected = expectations()
    expected["replace_scene"] = True
    provider = Provider()
    review(provider, expected)
    data = context(provider)
    assert data["scene_replaced"] is True
    assert all("scene" not in item for item in data["segments"])
    assert "不得要求恢复源场景" in provider.calls[0]["instructions"]


@pytest.mark.parametrize("marked_revised", [True, False])
def test_actual_final_prompt_takes_precedence_over_old_motion_and_timing(marked_revised: bool):
    expected = expectations()
    final_text = "最终确认：全程坐着讲解，不行走；台词：看看院子。"
    if marked_revised:
        expected.update(prompt_revised=True, prompt_text=final_text)
    provider = Provider()
    review(provider, expected, prompt_text=final_text)
    data = context(provider)
    assert data["final_prompt"] == final_text
    assert data["prompt_revised"] is True
    assert data["confirmed_script"] is None
    assert all(item["start_time"] == 0 and item["end_time"] == 8 for item in data["segments"])
    assert all("action" not in item and "motion" not in item for item in data["segments"])


@pytest.mark.parametrize(
    "bad_expected", [{}, {"shots": []}, {**expectations(), "output_duration_seconds": 30}]
)
def test_invalid_expectations_do_not_call_provider(bad_expected: dict):
    provider = Provider()
    result = review(provider, bad_expected)
    assert result.quality_status == "REPLICA_REVIEW_REQUIRED"
    assert result.issue_codes == ["REPLICA_REVIEW_INCOMPLETE"]
    assert not provider.calls


def test_provider_unavailability_is_safe_and_never_retried():
    provider = Provider(error=RuntimeError("provider unavailable"))
    assert review(provider) == unavailable_replica_review()
    assert len(provider.calls) == 1
    assert review(object()) == unavailable_replica_review()


def test_invalid_json_is_incomplete_not_a_positive_verdict():
    class InvalidProvider:
        def review_generated_replica(self, **kwargs):
            return SimpleNamespace(text="not JSON")

    result = review(InvalidProvider())
    assert result.issue_codes == ["REPLICA_REVIEW_INCOMPLETE"]
