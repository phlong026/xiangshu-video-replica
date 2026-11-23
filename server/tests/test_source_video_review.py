from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import Mock

import pytest

from app.analysis import (
    AnalysisProviderFailed,
    ApilioGemini,
    ProviderResponse,
    VideoCallBudget,
    _BudgetTracker,
    _default_analysis_payload,
    analyze_video,
)


def source_payload(*, with_facts: bool = True) -> dict[str, Any]:
    payload = _default_analysis_payload(4)
    for index, shot in enumerate(payload["shots"]):
        if not with_facts:
            shot.pop("motion", None)
            continue
        shot["facts"] = {
            "schema_version": "storyboard.facts.v1",
            "camera_shot_id": "c1",
            "entry_boundary": "VIDEO_START" if index == 0 else "CONTINUOUS",
            "subjects": [
                {"subject_id": "p1", "label": "主讲人", "kind": "PERSON", "visibility": "VISIBLE"}
            ],
            "primary_subject_id": "p1",
            "actions": [
                {
                    "subject_id": "p1",
                    "description": "主讲人连续向镜头方向行走",
                    "translation": "MOVING",
                    "gait": "WALKING",
                    "turning": "NONE",
                    "gesture": "NONE",
                    "direction": "toward_camera",
                    "path": "从庭院深处走近镜头",
                    "hand_detail": "双臂随步态自然摆动",
                }
            ],
            "camera": {"motion": "STATIC", "detail": "机位固定，庭院背景边缘不变"},
            "relative_motion": "人物逐步靠近镜头，画面占比增大",
            "evidence": [
                {
                    "source": "VIDEO",
                    "fields": [
                        "subjects",
                        "actions",
                        "camera",
                        "entry_boundary",
                        "relative_motion",
                    ],
                    "start_time": shot["start_time"],
                    "end_time": shot["end_time"],
                    "observation": "同一主讲人连续迈步靠近，背景边缘不变，段间画面连续",
                    "status": "CURRENT",
                },
                {
                    "source": "AUDIO",
                    "fields": ["spoken_text"],
                    "start_time": shot["start_time"],
                    "end_time": shot["end_time"],
                    "observation": "这段只听到环境声，没有可辨识口播",
                    "status": "CURRENT",
                },
            ],
        }
    return payload


def reviewed_payload(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = source_payload() if payload is None else payload
    return {
        "analysis": payload,
        "review_observations": [
            {
                "shot_id": shot["shot_id"],
                "start_time": shot["start_time"],
                "end_time": shot["end_time"],
                "visual_observation": "主讲人连续迈步靠近镜头，背景边缘保持不变",
                "audio_observation": "只有环境声，没有听到可辨识口播",
            }
            for shot in payload["shots"]
        ],
    }


class ReviewProvider:
    requires_https_video_url = True

    def __init__(self, initial: dict[str, Any], reviewed: Any = None) -> None:
        self.initial = initial
        self.reviewed = reviewed_payload() if reviewed is None else reviewed
        self.review_calls: list[dict[str, Any]] = []

    def analyze(self, **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(text=json.dumps(self.initial), raw={"model": "test"})

    def review_consistency(self, **kwargs: Any) -> ProviderResponse:
        self.review_calls.append(kwargs)
        if isinstance(self.reviewed, Exception):
            raise self.reviewed
        return ProviderResponse(text=json.dumps(self.reviewed), raw={"model": "test-review"})

    def repair_json(self, **kwargs: Any) -> ProviderResponse:
        raise AssertionError("事实缺项不能由纯文本修复补造")


def run_analysis(provider: Any):
    return analyze_video(
        video_uri="https://example.com/source.mp4", video_duration_seconds=4, provider=provider
    )


@pytest.mark.parametrize("initial_kind", ["consistent", "missing_motion", "unknown"])
def test_source_review_is_standard_for_every_usable_draft(initial_kind: str) -> None:
    initial = source_payload(with_facts=initial_kind != "missing_motion")
    if initial_kind == "unknown":
        initial["shots"][0]["facts"]["actions"][0].update(translation="UNKNOWN", gait="UNKNOWN")
    provider = ReviewProvider(initial)
    result = run_analysis(provider)
    assert len(provider.review_calls) == 1
    assert provider.review_calls[0]["video_uri"] == "https://example.com/source.mp4"
    assert result.analysis.source_review.status == "PASSED"
    assert result.analysis.source_review.attempted is True
    assert result.analysis.shots[0].motion.subject_motion_state == "WALKING"
    assert "initial" in result.provider_response_ref["raw"]


def test_provider_cannot_self_report_source_review_passed() -> None:
    initial = source_payload(with_facts=False)
    initial["source_review"] = {"status": "PASSED", "attempted": True, "issues": []}
    provider = Mock(spec=["requires_https_video_url", "analyze"])
    provider.requires_https_video_url = True
    provider.analyze.return_value = ProviderResponse(text=json.dumps(initial), raw={})
    result = run_analysis(provider)
    assert result.analysis.source_review.status == "UNAVAILABLE"
    assert result.analysis.source_review.attempted is False


@pytest.mark.parametrize(
    "failure", [TimeoutError("timeout"), {"analysis": {}}, {"status": "PASSED"}]
)
def test_failed_review_keeps_the_usable_initial_analysis(failure: Any) -> None:
    provider = ReviewProvider(source_payload(with_facts=False), failure)
    result = run_analysis(provider)
    assert len(provider.review_calls) == 1
    assert len(result.analysis.shots) == 2
    assert result.analysis.source_review.status in {"UNAVAILABLE", "INCOMPLETE"}
    assert result.analysis.source_review.attempted is True


@pytest.mark.parametrize(
    "problem", ["missing_shot", "out_of_range", "unobserved", "manual", "unknown"]
)
def test_review_requires_complete_source_video_evidence(problem: str) -> None:
    reviewed = reviewed_payload()
    if problem == "missing_shot":
        reviewed["review_observations"].pop()
    elif problem == "out_of_range":
        reviewed["review_observations"][0]["end_time"] = 99
    elif problem == "unobserved":
        reviewed["review_observations"][0]["visual_observation"] = "未观看视频，按描述推测符合"
    elif problem == "manual":
        for shot in reviewed["analysis"]["shots"]:
            shot["facts"]["evidence"][0]["source"] = "MANUAL"
    else:
        reviewed["analysis"]["shots"][0]["facts"]["actions"][0]["gait"] = "UNKNOWN"
    result = run_analysis(ReviewProvider(source_payload(with_facts=False), reviewed))
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert result.analysis.source_review.issues


def test_review_budget_reserves_time_and_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr("app.analysis.time.monotonic", lambda: now[0])
    tracker = _BudgetTracker(
        VideoCallBudget(attempt_deadline=510, source_review_timeout_seconds=120)
    )
    assert tracker.can_start(240)
    now[0] = 240
    assert not tracker.can_start(240)
    assert tracker.can_start(120, source_review=True)
    tracker.calls_used = 3
    assert not tracker.can_start(1)
    assert tracker.can_start(120, source_review=True)
    tracker.calls_used = 4
    assert not tracker.can_start(1, source_review=True)


def test_long_storyboard_reserves_review_without_changing_prompt_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import replica_prompt

    monkeypatch.setenv(replica_prompt.REPLICA_PROMPT_TIMEOUT_ENV, "99999")
    monkeypatch.setattr(
        replica_prompt, "get_video_analysis_provider", lambda conn, **kwargs: kwargs
    )
    normal = replica_prompt.build_long_video_provider(Mock())
    storyboard = replica_prompt.build_long_video_provider(Mock(), mode="storyboard")
    assert normal["transport"].timeout_seconds == 1260
    assert storyboard["transport"].timeout_seconds == 945
    assert storyboard["video_call_budget"].source_review_timeout_seconds == 300
    assert normal["video_call_budget"].source_review_timeout_seconds == 0


class QueueTransport:
    timeout_seconds = 1

    def __init__(self, values: list[Any]) -> None:
        self.values = values
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: Any, body: bytes) -> tuple[bytes, dict[str, str]]:
        self.requests.append(json.loads(body))
        value = self.values[len(self.requests) - 1]
        if isinstance(value, Exception):
            raise value
        return json.dumps(
            {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
        ).encode(), {}


def test_source_review_never_retries_or_falls_back_after_a_physical_call() -> None:
    transport = QueueTransport(
        [
            source_payload(with_facts=False),
            AnalysisProviderFailed("busy", http_status=429, failure_phase="HTTP"),
            reviewed_payload(),
        ]
    )
    provider = ApilioGemini(
        api_key="test",
        transport=transport,
        fallback_models=("backup",),
        video_call_budget=VideoCallBudget(
            attempt_deadline=float("inf"), source_review_timeout_seconds=120
        ),
    )
    result = run_analysis(provider)
    assert len(transport.requests) == 2
    assert result.analysis.source_review.status == "UNAVAILABLE"
    assert result.analysis.source_review.attempted is True
    assert any(
        item.get("type") == "image_url" for item in transport.requests[1]["messages"][1]["content"]
    )


def test_missing_facts_in_review_stays_incomplete_without_losing_analysis() -> None:
    result = run_analysis(
        ReviewProvider(
            source_payload(with_facts=False), reviewed_payload(source_payload(with_facts=False))
        )
    )
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert len(result.analysis.shots) == 2


@pytest.mark.parametrize(
    "bad_facts", [{"schema_version": "made-up"}, [], "broken", {"camera": {"motion": "STATIC"}}]
)
def test_invalid_paid_facts_are_preserved_as_hard_issues_without_text_repair(
    bad_facts: Any,
) -> None:
    from app.analysis import validate_shot_cards
    from app.storyboard_consistency import StoryboardConsistencyError, storyboard_consistency_issues

    initial = source_payload(with_facts=False)
    initial["shots"][0]["facts"] = bad_facts
    result = run_analysis(ReviewProvider(initial, TimeoutError("no review")))
    shot = result.analysis.shots[0].model_dump(mode="json")
    assert shot["facts"] == bad_facts
    assert any(
        issue["code"] == "FACTS_SCHEMA_INVALID" for issue in result.analysis.consistency_issues
    )
    assert result.analysis.source_review.status == "UNAVAILABLE"
    assert "facts" in result.provider_response_ref["raw"]["initial"]["text"]
    # 编译共用门禁仍能看到原始错误，不能凭删除 facts 绕过。
    assert any(
        issue["code"] == "FACTS_SCHEMA_INVALID" for issue in storyboard_consistency_issues([shot])
    )
    with pytest.raises(StoryboardConsistencyError):
        validate_shot_cards([shot], duration_seconds=4)


def test_invalid_review_facts_cannot_overwrite_the_usable_initial_draft() -> None:
    reviewed = reviewed_payload()
    reviewed["analysis"]["shots"][0]["facts"] = {"schema_version": "invented"}
    result = run_analysis(ReviewProvider(source_payload(), reviewed))
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert result.analysis.shots[0].facts["schema_version"] == "storyboard.facts.v1"


def test_unknown_facts_allow_null_legacy_person_count_and_boundary() -> None:
    initial = source_payload()
    initial["shots"][0]["facts"]["subjects"][0]["visibility"] = "UNKNOWN"
    initial["shots"][0]["facts"]["entry_boundary"] = "UNKNOWN"
    result = run_analysis(ReviewProvider(initial, TimeoutError("no review")))
    assert result.analysis.shots[0].person_count is None
    assert result.analysis.shots[0].segment_kind is None
    assert result.analysis.source_review.status == "UNAVAILABLE"


@pytest.mark.parametrize(
    "problem", ["partial_interval", "duplicate_shot", "audio_only", "unheard", "inferred_english"]
)
def test_review_does_not_pass_weak_or_fabricated_evidence(problem: str) -> None:
    reviewed = reviewed_payload()
    if problem == "partial_interval":
        reviewed["review_observations"][0]["end_time"] = 0.2
    elif problem == "duplicate_shot":
        reviewed["review_observations"].append(copy.deepcopy(reviewed["review_observations"][0]))
    elif problem == "audio_only":
        for shot in reviewed["analysis"]["shots"]:
            shot["facts"]["evidence"][0]["source"] = "AUDIO"
    elif problem == "unheard":
        reviewed["review_observations"][0]["audio_observation"] = "音轨不可用，无法判断"
    else:
        reviewed["review_observations"][0]["visual_observation"] = (
            "I have not watched the video; inferred from the prompt."
        )
    result = run_analysis(ReviewProvider(source_payload(), reviewed))
    assert result.analysis.source_review.status == "INCOMPLETE"


def long_payload(*, with_facts: bool = True) -> dict[str, Any]:
    from app.analysis import default_replica_storyboard_payload

    payload = default_replica_storyboard_payload(4)
    payload["shots"] = source_payload(with_facts=with_facts)["shots"]
    payload["spoken_script"] = []
    return payload


def run_long(provider: Any, *, callback: Any = None, mode: str = "storyboard") -> dict[str, Any]:
    from app.replica_prompt import (
        ReplicaPromptTaskLease,
        ReplicaPromptTaskWork,
        perform_replica_prompt_task,
    )

    return perform_replica_prompt_task(
        ReplicaPromptTaskWork(
            lease=ReplicaPromptTaskLease(
                "task", "project", "asset", "user", 4, "worker", mode=mode
            ),
            provider=provider,
            video_uri="https://example.com/source.mp4",
            asset_uri="asset://source",
            media_info={"duration_seconds": 4},
            mode=mode,
        ),
        on_provider_result=callback,
    )


@pytest.mark.parametrize("initial_kind", ["consistent", "missing_motion", "unknown"])
def test_long_storyboard_standard_review_runs_after_initial_cost_receipt(initial_kind: str) -> None:
    initial = long_payload(with_facts=initial_kind != "missing_motion")
    if initial_kind == "unknown":
        initial["shots"][0]["facts"]["camera"]["motion"] = "UNKNOWN"
    transport = QueueTransport([initial, reviewed_payload(long_payload())])
    provider = ApilioGemini(api_key="test", transport=transport)
    receipts: list[int] = []
    result = run_long(provider, callback=lambda: receipts.append(len(transport.requests)))
    assert receipts == [1]
    assert len(transport.requests) == 2
    assert result["source_review"]["status"] == "PASSED"
    assert result["source_review"]["attempted"] is True
    assert result["shots"][0]["motion"]["subject_motion_state"] == "WALKING"
    assert transport.requests[1]["messages"][1]["content"][1]["image_url"]["url"].endswith(
        "source.mp4"
    )


def test_long_storyboard_review_failure_preserves_initial_while_prompt_mode_stays_single_call() -> (
    None
):
    transport = QueueTransport([long_payload(), TimeoutError("no review")])
    result = run_long(ApilioGemini(api_key="test", transport=transport))
    assert result["source_review"]["status"] == "UNAVAILABLE"
    assert len(result["shots"]) == 2
    prompt_transport = QueueTransport([long_payload()])
    prompt = run_long(ApilioGemini(api_key="test", transport=prompt_transport), mode="prompt")
    assert len(prompt_transport.requests) == 1
    assert "source_review" not in prompt


@pytest.mark.parametrize(
    "mode,initial_timeout,review_timeout,budget",
    [("analysis", 240, 120, 510), ("storyboard", 945, 300, 1380)],
)
def test_real_transport_uses_reserved_shorter_timeout_and_one_cost_boundary(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    initial_timeout: float,
    review_timeout: float,
    budget: float,
) -> None:
    from contextlib import contextmanager

    from app.analysis import UrllibApilioChatTransport

    now = [0.0]
    monkeypatch.setattr("app.analysis.time.monotonic", lambda: now[0])
    calls: list[float] = []
    meter_events: list[str] = []

    @contextmanager
    def meter(service: str):
        meter_events.append(service)
        yield

    def physical(request: Any, **kwargs: Any):
        calls.append(kwargs["timeout"])
        payload = source_payload() if mode == "analysis" else long_payload()
        value = payload if len(calls) == 1 else reviewed_payload(payload)
        now[0] += initial_timeout if len(calls) == 1 else 1
        return (
            json.dumps(
                {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}
            ).encode(),
            {},
            200,
        )

    monkeypatch.setattr("app.analysis.meter_call", meter)
    monkeypatch.setattr("app.analysis.recorded_urlopen", physical)
    provider = ApilioGemini(
        api_key="test",
        transport=UrllibApilioChatTransport(timeout_seconds=initial_timeout),
        video_call_budget=VideoCallBudget(
            attempt_deadline=budget, source_review_timeout_seconds=review_timeout
        ),
    )
    result = (
        run_analysis(provider).analysis.model_dump(mode="json")
        if mode == "analysis"
        else run_long(provider)
    )
    assert calls == [initial_timeout, review_timeout]
    assert meter_events == ["quality_inspection"]
    assert result["source_review"]["status"] == "PASSED"


def test_budget_exhaustion_never_claims_review_attempt_or_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr("app.analysis.time.monotonic", lambda: now[0])
    transport = QueueTransport([source_payload()])
    provider = ApilioGemini(
        api_key="test",
        transport=transport,
        video_call_budget=VideoCallBudget(attempt_deadline=510, source_review_timeout_seconds=120),
    )
    result = analyze_video(
        video_uri="https://example.com/source.mp4",
        video_duration_seconds=4,
        provider=provider,
        on_provider_result=lambda: now.__setitem__(0, 500),
    )
    assert len(transport.requests) == 1
    assert result.analysis.source_review.status == "UNAVAILABLE"
    assert result.analysis.source_review.attempted is False


def test_source_audit_is_bounded_redacted_and_public_refs_neutral() -> None:
    from app.analysis import ANALYSIS_REPAIR_MAX_CHARS, _source_response_audit
    from app.analysis_routes import external_version_payload

    response = ProviderResponse(
        text="源片 https://example.com/private?secret=1 " + "观" * ANALYSIS_REPAIR_MAX_CHARS,
        raw={
            "provider": "apilio_gemini",
            "response_id": "safe-id",
            "headers": {"Authorization": "secret"},
            "signed_url": "https://secret",
        },
    )
    audit = _source_response_audit(response)
    assert len(audit["text"]) == ANALYSIS_REPAIR_MAX_CHARS
    assert audit["truncated"] is True
    assert "secret" not in json.dumps(audit)
    payload = {"provider_response_ref": {"raw": {"initial": audit, "source_review": audit}}}
    public = external_version_payload(payload)
    assert (
        public["provider_response_ref"]["raw"]["initial"]["ref"]["provider"] == "analysis_gateway"
    )
    assert payload["provider_response_ref"]["raw"]["initial"]["ref"]["provider"] == "apilio_gemini"


def test_analysis_does_not_receive_target_people_scene_or_copy() -> None:
    transport = QueueTransport([source_payload(), reviewed_payload()])
    provider = ApilioGemini(api_key="test", transport=transport)
    analyze_video(
        video_uri="https://example.com/source.mp4",
        video_duration_seconds=4,
        provider=provider,
        generation_context={
            "mode": None,
            "context_hash": "test",
            "duration_seconds": 30,
            "new_script_text": "TARGET COPY",
            "replace_scene": "TARGET SCENE",
            "generation_assets": [{"name": "TARGET PERSON"}],
            "media_info": {},
        },
        generation_media=[
            {"type": "image_url", "image_url": {"url": "https://example.com/target.png"}}
        ],
    )
    requests = json.dumps(transport.requests)
    assert "TARGET" not in requests and "target.png" not in requests
    assert all(
        "storyboard.facts.v1" in request["messages"][0]["content"] for request in transport.requests
    )


@pytest.mark.parametrize("change", ["none", "legacy_cache", "facts", "speech"])
def test_manual_card_preserves_review_only_when_normalized_facts_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from app import analysis as module

    analysis = run_analysis(ReviewProvider(source_payload())).analysis.model_dump(mode="json")
    shots = copy.deepcopy(analysis["shots"])
    if change == "legacy_cache":
        shots[0]["action"] = "stale cached action"
        shots[0]["motion"]["subject_motion_state"] = "STATIC"
    elif change == "facts":
        shots[0]["facts"]["actions"][0]["path"] = "从画面远处连续走到镜头前"
    elif change == "speech":
        shots[0]["spoken_text"] = "确认后的台词"
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        module, "insert_version", lambda *args, **kwargs: saved.append(kwargs["payload"])
    )
    validated = module.validate_shot_cards(shots, duration_seconds=4)
    module.create_shot_card_version(
        None,
        analysis_version={
            "id": "a1",
            "project_id": "p1",
            "asset_id": "asset1",
            "payload_json": json.dumps({"analysis": analysis}),
        },
        created_by_user_id="u1",
        shots=validated,
        manually_reviewed=True,
    )
    review = saved[0]["source_review"]
    if change in {"none", "legacy_cache"}:
        assert review["status"] == "PASSED"
        assert review["attempted"] is True
    else:
        assert review["status"] == "INCOMPLETE"
        assert review["attempted"] is False
        assert review["issues"][0]["code"] == "SOURCE_REVIEW_STALE"
        evidence = saved[0]["shots"][0]["facts"]["evidence"]
        assert evidence[0]["status"] == ("STALE" if change == "facts" else "CURRENT")
        assert evidence[1]["status"] == ("STALE" if change == "speech" else "CURRENT")
        assert all(e["status"] == "CURRENT" for e in saved[0]["shots"][1]["facts"]["evidence"])


@pytest.mark.parametrize("problem", ["unobserved", "hard_conflict"])
def test_failed_review_cannot_replace_a_consistent_initial_draft(problem: str) -> None:
    initial = source_payload()
    reviewed = reviewed_payload()
    if problem == "unobserved":
        reviewed["analysis"]["shots"][0]["facts"]["camera"] = {
            "motion": "FOLLOW",
            "detail": "摄影机向前跟随",
        }
        reviewed["review_observations"][0]["visual_observation"] = "未观看视频，根据描述推测"
    else:
        reviewed["analysis"]["shots"][0]["facts"]["actions"][0]["subject_id"] = "missing-subject"
    result = run_analysis(ReviewProvider(initial, reviewed))
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert result.analysis.consistency_issues == []
    assert result.analysis.shots[0].facts["camera"]["motion"] == "STATIC"
    assert result.analysis.shots[0].facts["actions"][0]["subject_id"] == "p1"
    assert "source_review" in result.provider_response_ref["raw"]


def test_time_edit_expires_only_the_edited_segment_evidence() -> None:
    from app.analysis import _stale_changed_shot_evidence

    original = source_payload()["shots"]
    modified = copy.deepcopy(original)
    modified[0]["end_time"] -= 0.1
    _stale_changed_shot_evidence(original, modified)
    assert all(e["status"] == "STALE" for e in modified[0]["facts"]["evidence"])
    assert all(e["status"] == "CURRENT" for e in modified[1]["facts"]["evidence"])


@pytest.mark.parametrize("missing", ["facts", "evidence"])
def test_incomplete_review_cannot_drop_initial_facts_or_evidence(missing: str) -> None:
    reviewed = reviewed_payload(source_payload(with_facts=missing != "facts"))
    if missing == "evidence":
        reviewed["analysis"]["shots"][0]["facts"]["evidence"] = []
    result = run_analysis(ReviewProvider(source_payload(), reviewed))
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert result.analysis.shots[0].facts["schema_version"] == "storyboard.facts.v1"
    assert len(result.analysis.shots[0].facts["evidence"]) == 2
    assert result.analysis.shots[0].motion.subject_motion_state == "WALKING"


def test_observed_review_can_preserve_a_new_local_unknown_without_discarding_facts() -> None:
    reviewed = reviewed_payload()
    reviewed["analysis"]["shots"][0]["facts"]["camera"] = {
        "motion": "UNKNOWN",
        "detail": "背景有轻微晃动，机位运动无法判断",
    }
    result = run_analysis(ReviewProvider(source_payload(), reviewed))
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert result.analysis.shots[0].facts["camera"]["motion"] == "UNKNOWN"
    assert result.analysis.shots[0].facts["actions"][0]["gait"] == "WALKING"


@pytest.mark.parametrize("changed", ["gait", "camera", "subject_kind"])
def test_manual_evidence_staleness_is_scoped_to_subject_and_field(changed: str) -> None:
    from app.analysis import _stale_changed_shot_evidence

    original = source_payload()["shots"]
    facts = original[0]["facts"]
    facts["subjects"].append(
        {"subject_id": "p2", "label": "同行者", "kind": "PERSON", "visibility": "VISIBLE"}
    )
    facts["actions"].append({**facts["actions"][0], "subject_id": "p2"})
    fields = [
        "actions.p1.gait",
        "actions.p1.hand_detail",
        "actions.p2.gait",
        "camera.motion",
        "camera.detail",
        "subjects.p1.kind",
        "subjects.p1.visibility",
    ]
    facts["evidence"] = [{**facts["evidence"][0], "fields": [field]} for field in fields]
    modified = copy.deepcopy(original)
    if changed == "gait":
        modified[0]["facts"]["actions"][0]["gait"] = "RUNNING"
        expired = {"actions.p1.gait"}
    elif changed == "camera":
        modified[0]["facts"]["camera"]["motion"] = "FOLLOW"
        expired = {"camera.motion"}
    else:
        modified[0]["facts"]["subjects"][0]["kind"] = "OBJECT"
        expired = {
            "subjects.p1.kind",
            "subjects.p1.visibility",
            "actions.p1.gait",
            "actions.p1.hand_detail",
        }
    _stale_changed_shot_evidence(original, modified)
    assert {
        e["fields"][0] for e in modified[0]["facts"]["evidence"] if e["status"] == "STALE"
    } == expired
    assert all(e["status"] == "CURRENT" for e in modified[1]["facts"]["evidence"])


@pytest.mark.parametrize("boundary", [1.5, 2.5])
def test_long_review_cannot_pass_or_replace_draft_with_timeline_gap_or_overlap(
    boundary: float,
) -> None:
    initial = long_payload()
    proposed = long_payload()
    proposed["shots"][1]["start_time"] = boundary
    for evidence in proposed["shots"][1]["facts"]["evidence"]:
        evidence["start_time"] = boundary
    transport = QueueTransport([initial, reviewed_payload(proposed)])
    result = run_long(ApilioGemini(api_key="test", transport=transport))
    assert result["source_review"]["status"] == "INCOMPLETE"
    assert result["shots"][1]["start_time"] == 2


def test_review_cannot_expand_an_existing_evidence_gap_under_the_same_issue_code() -> None:
    initial = source_payload()
    initial["shots"][0]["facts"]["evidence"][0]["fields"].remove("camera")
    proposed = copy.deepcopy(initial)
    proposed["shots"][0]["facts"]["evidence"] = []
    result = run_analysis(ReviewProvider(initial, reviewed_payload(proposed)))
    assert result.analysis.source_review.status == "INCOMPLETE"
    assert len(result.analysis.shots[0].facts["evidence"]) == 2
    assert "camera" not in result.analysis.shots[0].facts["evidence"][0]["fields"]
    assert "source_review" in result.provider_response_ref["raw"]
