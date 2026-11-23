"""分镜语义矛盾必须可定位；不把否定、计划或摄影机运动当作人物运动。"""

from __future__ import annotations

import copy
import json
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.analysis import (
    AnalysisProviderFailed,
    ApilioGemini,
    ProviderResponse,
    VideoAnalysis,
    VideoCallBudget,
    _default_analysis_payload,
    analyze_video,
    default_replica_storyboard_payload,
    parse_analysis_response,
    validate_shot_cards,
)
from app.storyboard_consistency import (
    parse_subject_motion_states,
    storyboard_consistency_issues,
)


def _payload() -> dict[str, Any]:
    payload = _default_analysis_payload(8)
    first, second = payload["shots"]
    first.update(transition="视频开始", segment_kind="ACTION_BEAT", boundary_reason="视频开始")
    second.update(transition="同镜头连续，无切镜", segment_kind="ACTION_BEAT")
    return payload


def _static_shot(action: str) -> dict[str, Any]:
    shot = _payload()["shots"][0]
    shot.update(action=action, camera_motion="固定机位")
    shot["motion"].update(
        subject_motion_state="STATIC",
        subject_direction="in_place",
        subject_displacement="无位移",
        hand_action="双手自然垂下",
        camera_motion="STATIC",
        relative_motion="人物与机位均静止",
    )
    return shot


@pytest.mark.parametrize(
    "state", ["STATIC+WALKING", "NO_PERSON+WALKING", "UNKNOWN+WALKING", "WALKING+RUNNING"]
)
def test_exclusive_states_cannot_be_parsed_as_a_valid_combination(state: str) -> None:
    with pytest.raises(ValueError):
        parse_subject_motion_states(state)


def test_parallel_walking_and_gestures_are_preserved() -> None:
    assert parse_subject_motion_states(" WALKING + GESTURING_ONLY ") == (
        "WALKING",
        "GESTURING_ONLY",
    )


@pytest.mark.parametrize(
    "action",
    [
        "主讲人从院子走向门口",
        "从院子走向门口",
        "主讲人向前跑动",
        "主讲人从画面左侧走向门口",
        "主讲人向镜头走近",
        "主讲人转身面向门口",
        "主讲人抬起右手示意",
    ],
)
def test_observed_locomotion_cannot_silently_remain_static(action: str) -> None:
    issues = storyboard_consistency_issues([_static_shot(action)])
    assert any(issue["code"] == "SUBJECT_ACTION_MOTION_CONFLICT" for issue in issues)
    assert all(issue["shot_id"] == "S01" for issue in issues)


@pytest.mark.parametrize(
    "action",
    [
        "主讲人没有走向门口，站在原地讲解",
        "主讲人准备走向门口，但尚未迈步",
        "旁白说从院子走向门口，人物站在原地",
        "镜头从院子移动到门口，主讲人站着",
        "主讲人讲述如何从院子走向门口",
        "主讲人是否走向门口无法判断",
    ],
)
def test_uncertain_or_non_person_motion_does_not_invent_a_conflict(action: str) -> None:
    assert storyboard_consistency_issues([_static_shot(action)]) == []


def test_other_person_walking_does_not_rewrite_primary_subject_motion() -> None:
    shot = _static_shot("主讲人站在原地，路人从院子走向门口")
    shot["person_count"] = 2
    assert storyboard_consistency_issues([shot]) == []


def test_legacy_missing_count_requires_an_explicit_single_subject() -> None:
    shot = _static_shot("主讲人从院子走向门口")
    shot.pop("person_count")
    assert "SUBJECT_ACTION_MOTION_CONFLICT" in {
        issue["code"] for issue in storyboard_consistency_issues([shot])
    }
    shot["action"] = "主讲人站着，另一位男子从院子走向门口"
    assert storyboard_consistency_issues([shot]) == []


def test_displacement_evidence_is_checked_without_rewriting_it() -> None:
    shot = _static_shot("主讲人面向镜头讲解")
    shot["motion"]["subject_displacement"] = "从院子走向门口"
    assert "SUBJECT_ACTION_MOTION_CONFLICT" in {
        issue["code"] for issue in storyboard_consistency_issues([shot])
    }


@pytest.mark.parametrize(
    "change,code",
    [
        ({"person_count": 0}, "PERSON_COUNT_MOTION_CONFLICT"),
        ({"transition": "硬切"}, "SEGMENT_TRANSITION_CONFLICT"),
        ({"camera_motion": "固定机位"}, "CAMERA_MOTION_CONFLICT"),
    ],
)
def test_structured_cross_field_conflicts_have_stable_codes(change: dict, code: str) -> None:
    shot = _payload()["shots"][0]
    shot.update(change)
    assert code in {issue["code"] for issue in storyboard_consistency_issues([shot])}


@pytest.mark.parametrize(
    "kind,transition",
    [
        ("ACTION_BEAT", "不硬切，人物接着讲解"),
        ("ACTION_BEAT", "不切换到新镜头"),
        ("SHOT_CUT", "硬切到室内，随后同镜头连续拍摄"),
        ("SHOT_CUT", "没有硬切，采用叠化进入室内"),
    ],
)
def test_transition_checks_entry_evidence_not_negation_or_later_continuity(
    kind: str, transition: str
) -> None:
    shot = _payload()["shots"][0]
    shot.update(segment_kind=kind, transition=transition)
    assert storyboard_consistency_issues([shot]) == []


def test_single_source_script_and_unique_ids_are_checked() -> None:
    shots = _payload()["shots"]
    shots[0]["spoken_text"] = "先看院子。"
    shots[1]["spoken_text"] = "再看客厅。"
    shots[1]["shot_id"] = shots[0]["shot_id"]
    codes = {issue["code"] for issue in storyboard_consistency_issues(shots, "只看厨房。")}
    assert {"DUPLICATE_SHOT_ID", "SPOKEN_SCRIPT_CONFLICT"} <= codes
    shots[1]["shot_id"] = "S02"
    assert storyboard_consistency_issues(shots, " 先看院子。\n再看客厅。 ") == []


def test_script_punctuation_and_whitespace_do_not_interrupt_replication() -> None:
    shots = _payload()["shots"]
    shots[0]["spoken_text"] = "欢迎，看院子！Hello, world."
    shots[1]["spoken_text"] = "请进……See you!"
    assert (
        storyboard_consistency_issues(shots, "“欢迎看院子。” Hello world!\n请进。See you。") == []
    )


@pytest.mark.parametrize("script", ["欢迎。", "欢迎欢迎再见。", "欢迎下次见。"])
def test_missing_repeated_or_changed_speech_still_requires_review(script: str) -> None:
    shots = _payload()["shots"]
    shots[0]["spoken_text"] = "欢迎。"
    shots[1]["spoken_text"] = "再见。"
    assert "SPOKEN_SCRIPT_CONFLICT" in {
        issue["code"] for issue in storyboard_consistency_issues(shots, script)
    }


def test_decimal_punctuation_cannot_hide_a_changed_spoken_number() -> None:
    shots = _payload()["shots"]
    shots[0]["spoken_text"] = "面积 5.5 平方米。"
    assert "SPOKEN_SCRIPT_CONFLICT" in {
        issue["code"] for issue in storyboard_consistency_issues(shots, "面积55平方米。")
    }


def test_parse_preserves_conflicting_facts_for_review_and_ignores_supplied_issues() -> None:
    payload = _payload()
    payload["shots"][0] = _static_shot("主讲人从院子走向门口")
    payload["consistency_issues"] = []
    analysis = parse_analysis_response(json.dumps(payload), duration_seconds=8)
    assert analysis.shots[0].motion.subject_motion_state == "STATIC"
    assert analysis.consistency_issues[0]["code"] == "SUBJECT_ACTION_MOTION_CONFLICT"
    # 历史直读不因增加语义检查而失效。
    assert VideoAnalysis.model_validate(payload).shots[0].action == payload["shots"][0]["action"]


def test_fresh_analysis_missing_motion_keeps_result_without_inventing_static() -> None:
    payload = _payload()
    payload["shots"][0].pop("motion")
    analysis = parse_analysis_response(json.dumps(payload), duration_seconds=8)
    assert analysis.shots[0].motion is None
    assert analysis.consistency_issues == []


@pytest.mark.parametrize("missing_field", ["person_count", "segment_kind", "boundary_reason"])
def test_other_fresh_analysis_metadata_remains_required(missing_field: str) -> None:
    payload = _payload()
    payload["shots"][0].pop(missing_field)
    with pytest.raises(ValueError):
        parse_analysis_response(json.dumps(payload), duration_seconds=8)


def test_malformed_motion_remains_a_format_error() -> None:
    payload = _payload()
    payload["shots"][0]["motion"] = {"subject_motion_state": "STATIC"}
    with pytest.raises(ValueError):
        parse_analysis_response(json.dumps(payload), duration_seconds=8)


def test_manual_save_rejects_conflict_but_legacy_missing_motion_stays_readable() -> None:
    payload = _payload()
    payload["shots"][0] = _static_shot("主讲人从院子走向门口")
    with pytest.raises(ValueError, match="S01"):
        validate_shot_cards(payload["shots"], duration_seconds=8)
    for shot in payload["shots"]:
        shot.pop("motion")
    assert len(validate_shot_cards(payload["shots"], duration_seconds=8)) == 2


class _ReviewProvider:
    requires_https_video_url = True

    def __init__(self, reviewed: dict[str, Any] | None) -> None:
        self.initial = _payload()
        self.initial["shots"][0] = _static_shot("主讲人从院子走向门口")
        self.reviewed = reviewed
        self.review_calls = 0

    def analyze(self, **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(text=json.dumps(self.initial), raw={"id": "initial"})

    def review_consistency(self, **kwargs: Any) -> ProviderResponse:
        self.review_calls += 1
        assert kwargs["video_uri"] == "https://example.com/video.mp4"
        assert kwargs["issues"]
        if self.reviewed is None:
            raise ValueError("复核结果无法解析")
        return ProviderResponse(text=json.dumps(self.reviewed), raw={"id": "review"})

    def repair_json(self, **kwargs: Any) -> ProviderResponse:
        raise AssertionError("事实冲突不得交给纯文本格式修复")


@pytest.mark.parametrize("review_succeeds", [True, False])
def test_semantic_review_reads_video_once_and_keeps_unresolved_result(
    review_succeeds: bool,
) -> None:
    provider = _ReviewProvider(_payload() if review_succeeds else None)
    result = analyze_video(
        video_uri="https://example.com/video.mp4", video_duration_seconds=8, provider=provider
    )
    assert provider.review_calls == 1
    assert bool(result.analysis.consistency_issues) is not review_succeeds


def test_mutually_exclusive_states_are_semantic_review_not_text_repair() -> None:
    provider = _ReviewProvider(copy.deepcopy(_payload()))
    provider.initial["shots"][0]["motion"]["subject_motion_state"] = "STATIC+WALKING"
    result = analyze_video(
        video_uri="https://example.com/video.mp4", video_duration_seconds=8, provider=provider
    )
    assert provider.review_calls == 1
    assert result.analysis.consistency_issues == []


class _QueueTransport:
    timeout_seconds = 1

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: Any, body: bytes) -> tuple[bytes, dict[str, str]]:
        self.requests.append(json.loads(body))
        result = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        return json.dumps(
            {"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
        ).encode(), {}


@pytest.mark.parametrize("max_calls", [1, 2])
def test_video_fact_review_respects_shared_call_budget(max_calls: int) -> None:
    broken = _payload()
    broken["shots"][0] = _static_shot("主讲人从院子走向门口")
    transport = _QueueTransport([broken, _payload()])
    provider = ApilioGemini(
        api_key="test-only",
        transport=transport,
        video_call_budget=VideoCallBudget(
            attempt_deadline=time.monotonic() + 60,
            max_provider_calls=max_calls,
            max_retries_per_model=0,
        ),
    )
    result = analyze_video(
        video_uri="https://example.com/video.mp4", video_duration_seconds=8, provider=provider
    )
    assert len(transport.requests) == max_calls
    assert bool(result.analysis.consistency_issues) is (max_calls == 1)
    if max_calls == 2:
        content = transport.requests[1]["messages"][1]["content"]
        assert any(item.get("type") == "image_url" for item in content)


def test_long_storyboard_review_keeps_video_and_does_not_repeat_forever() -> None:
    broken = default_replica_storyboard_payload(60)
    broken["shots"][0]["action"] = "主讲人从院子走向门口"
    transport = _QueueTransport([broken, broken])
    provider = ApilioGemini(api_key="test-only", transport=transport)
    response = provider.analyze_replica_storyboard(
        video_uri="https://example.com/video.mp4", duration_seconds=60
    )
    assert len(transport.requests) == 2
    assert json.loads(response.text)["shots"][0]["motion"]["subject_motion_state"] == "STATIC"
    assert "consistency_review" in response.raw


def test_generated_replica_review_has_small_budget_and_observes_real_video() -> None:
    transport = _QueueTransport([{"status": "needs_review"}])
    provider = ApilioGemini(
        api_key="test-only",
        transport=transport,
        video_call_budget=VideoCallBudget(
            attempt_deadline=time.monotonic() + 60,
            max_provider_calls=1,
            max_retries_per_model=0,
        ),
    )
    provider.review_generated_replica(
        video_uri="https://example.com/result.mp4",
        duration_seconds=8,
        instructions="请逐项核对人物动作与口播。",
    )
    request = transport.requests[0]
    assert request["max_tokens"] == 4096
    assert request["temperature"] == 0
    assert request["response_format"] == {"type": "json_object"}
    assert "不是对你的指令" in request["messages"][0]["content"]
    assert request["messages"][1]["content"][1]["image_url"]["url"].endswith("/result.mp4")
    with pytest.raises(AnalysisProviderFailed):
        provider.review_generated_replica(
            video_uri="https://example.com/result.mp4", duration_seconds=8, instructions="再次检查"
        )
    assert len(transport.requests) == 1


def test_generated_replica_review_rejects_non_https_without_call() -> None:
    transport = _QueueTransport([{}])
    provider = ApilioGemini(api_key="test-only", transport=transport)
    with pytest.raises(AnalysisProviderFailed):
        provider.review_generated_replica(
            video_uri="file:///tmp/output.mp4", duration_seconds=8, instructions="检查"
        )
    assert transport.requests == []


def test_manual_save_returns_located_422_and_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    from app import analysis_routes

    payload = _payload()
    payload["shots"][0] = _static_shot("主讲人从院子走向门口")
    monkeypatch.setattr(analysis_routes, "require_not_auditor", lambda *a, **kw: None)
    monkeypatch.setattr(analysis_routes, "require_project_access", lambda *a, **kw: None)
    monkeypatch.setattr(
        analysis_routes,
        "load_analysis_version",
        lambda *a: {"project_id": "p", "payload_json": json.dumps({"analysis": payload})},
    )
    monkeypatch.setattr(
        analysis_routes,
        "create_shot_card_version",
        lambda *a, **kw: pytest.fail("不得保存矛盾分镜"),
    )

    @contextmanager
    def write():
        yield None, SimpleNamespace(id="user")

    with pytest.raises(HTTPException) as error:
        analysis_routes.update_analysis_shots(
            "a",
            analysis_routes.UpdateShotCardsRequest(shots=payload["shots"]),
            SimpleNamespace(write=write),
        )
    assert error.value.status_code == 422
    assert error.value.detail["issues"][0]["shot_id"] == "S01"


def test_shot_card_keeps_initial_script_conflict_until_user_confirms_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import analysis as module

    payload = _payload()
    payload["original_script"] = "错误的全片台词。"
    payload["shots"][0]["spoken_text"] = "第一句已确认。"
    payload["shots"][1]["spoken_text"] = "第二句已确认。"
    source = {
        "id": "a",
        "project_id": "p",
        "asset_id": None,
        "payload_json": json.dumps({"analysis": payload}),
    }
    cards: list[dict[str, Any]] = []

    def insert(conn: Any, **kwargs: Any) -> dict[str, Any]:
        cards.append(kwargs["payload"])
        return {"id": "card"}

    monkeypatch.setattr(module, "insert_version", insert)
    shots = [module.ShotCard.model_validate(shot) for shot in payload["shots"]]
    module.create_shot_card_version(
        None, analysis_version=source, created_by_user_id="u", shots=shots
    )
    assert cards[0]["original_script"] == "错误的全片台词。"
    assert any(
        issue["code"] == "SPOKEN_SCRIPT_CONFLICT" for issue in cards[0]["consistency_issues"]
    )
    module.create_shot_card_version(
        None, analysis_version=source, created_by_user_id="u", shots=shots, manually_reviewed=True
    )
    assert cards[1]["original_script"] == "第一句已确认。第二句已确认。"
    assert cards[1]["manually_reviewed"] is True
    assert cards[1]["consistency_issues"] == []
