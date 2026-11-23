"""规范化事实绑定主体与时间；兼容投影不能制造新的观察结论。"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from app.storyboard_facts import (
    ShotFacts,
    normalize_storyboard_shots,
    render_storyboard_motion,
    storyboard_facts_issues,
    storyboard_facts_review_gaps,
    subject_motion_phrase,
)


def action(subject_id: str = "p1", **changes):
    return {
        "subject_id": subject_id,
        "translation": "MOVING",
        "gait": "WALKING",
        "turning": "NONE",
        "gesture": "GESTURING",
        "direction": "lateral",
        "path": "从院子走向门口",
        "hand_detail": "抬起右手示意",
        **changes,
    }


def facts_shot(**changes):
    return {
        "shot_id": "s1",
        "start_time": 0,
        "end_time": 4,
        "spoken_text": "",
        "action": "过期的静止描述",
        "person_count": 0,
        "motion": {"subject_motion_state": "STATIC"},
        "facts": {
            "schema_version": "storyboard.facts.v1",
            "camera_shot_id": "c1",
            "entry_boundary": "VIDEO_START",
            "subjects": [
                {
                    "subject_id": "p1",
                    "label": "主讲人",
                    "kind": "PERSON",
                    "visibility": "VISIBLE",
                }
            ],
            "primary_subject_id": "p1",
            "actions": [action()],
            "camera": {"motion": "STATIC", "detail": "固定机位"},
            "relative_motion": "人物向画面右侧移动，大小基本不变",
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
                    "start_time": 0,
                    "end_time": 4,
                    "observation": "主讲人从院子走向门口并抬右手；背景固定，无剪辑切换。",
                }
            ],
        },
        **changes,
    }


def codes(shots):
    return {issue["code"] for issue in storyboard_facts_issues(shots)}


def test_facts_override_stale_projection_without_mutating_the_input():
    shot = facts_shot()
    original = copy.deepcopy(shot)
    normalized = normalize_storyboard_shots([shot])[0]

    assert shot == original
    assert normalized["person_count"] == 1
    assert normalized["subject"] == "主讲人"
    assert normalized["motion"]["subject_motion_state"] == "WALKING+GESTURING_ONLY"
    assert normalized["segment_kind"] == "ACTION_BEAT"
    assert normalized["transition"] == "视频开始"
    assert "过期" not in normalized["action"]
    assert "行走" in render_storyboard_motion(shot)
    assert "手势" in render_storyboard_motion(shot)
    assert not storyboard_facts_issues([shot])
    assert not storyboard_facts_review_gaps([shot])


def test_legacy_data_is_not_upgraded_or_filled_in():
    legacy = {"shot_id": "old", "action": "人物走向门口", "person_count": None}
    copied = normalize_storyboard_shots([legacy])
    assert copied == [legacy]
    assert copied[0] is not legacy
    assert render_storyboard_motion(legacy) is None
    assert not storyboard_facts_issues([legacy])
    assert "FACTS_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([legacy])}


@pytest.mark.parametrize("invalid", [{}, [], "broken", {"schema_version": "future.v9"}])
def test_invalid_facts_cannot_fall_back_to_legacy(invalid):
    shot = facts_shot(facts=invalid)
    with pytest.raises(ValidationError):
        normalize_storyboard_shots([shot])
    assert "FACTS_SCHEMA_INVALID" in codes([shot])


def test_unknown_presence_is_not_zero_people_or_static():
    shot = facts_shot()
    shot["facts"]["subjects"][0]["visibility"] = "UNKNOWN"
    shot["facts"]["actions"] = []
    normalized = normalize_storyboard_shots([shot])[0]
    assert normalized["person_count"] is None
    assert normalized["motion"]["subject_motion_state"] == "UNKNOWN"
    assert not storyboard_facts_issues([shot])
    assert storyboard_facts_review_gaps([shot])


def test_known_displacement_with_unknown_gait_does_not_become_static():
    shot = facts_shot()
    shot["facts"]["actions"] = [action(gait="UNKNOWN", gesture="NONE", hand_detail="双手下垂")]
    normalized = normalize_storyboard_shots([shot])[0]
    assert normalized["motion"]["subject_motion_state"] == "MOVING"
    assert "移动" in render_storyboard_motion(shot)
    assert "行走" not in subject_motion_phrase(normalized["motion"])


def test_actor_identity_is_independent_of_the_number_of_people():
    shot = facts_shot()
    shot["facts"]["subjects"].append(
        {"subject_id": "p2", "label": "路人", "kind": "PERSON", "visibility": "PARTIAL"}
    )
    shot["facts"]["actions"] = [
        action(
            "p1",
            translation="STATIONARY",
            gait="NONE",
            gesture="NONE",
            direction="in_place",
            path="站位不变",
            hand_detail="双手下垂",
        ),
        action("p2", gesture="NONE", hand_detail="双手自然摆动"),
    ]
    normalized = normalize_storyboard_shots([shot])[0]
    assert normalized["person_count"] == 2
    assert normalized["motion"]["subject_motion_state"] == "STATIC"
    assert not storyboard_facts_issues([shot])
    assert "路人" in normalized["action"] and "行走" in normalized["action"]


def test_current_actor_observation_conflicts_are_preserved_for_review():
    shot = facts_shot()
    shot["facts"]["subjects"].append(
        {"subject_id": "p2", "label": "配角", "kind": "PERSON", "visibility": "VISIBLE"}
    )
    shot["facts"]["actions"][0].update(
        translation="STATIONARY",
        gait="NONE",
        gesture="NONE",
        direction="in_place",
        description="主讲人从院子走向门口",
        hand_detail="双手自然下垂",
    )
    normalized = normalize_storyboard_shots([shot])[0]
    assert "FACTS_ACTION_CONFLICT" in codes([normalized])
    assert normalized["facts"]["actions"][0]["description"] == "主讲人从院子走向门口"


def test_provider_observations_only_bind_unambiguous_primary_clauses():
    shot = facts_shot(action="主讲人站在原地；路人从院子走向门口")
    shot["facts"]["subjects"].append(
        {"subject_id": "p2", "label": "路人", "kind": "PERSON", "visibility": "VISIBLE"}
    )
    observed = normalize_storyboard_shots([shot], preserve_observations=True)[0]
    assert observed["facts"]["actions"][0]["description"] == "主讲人站在原地"
    manual = normalize_storyboard_shots([shot])[0]
    assert manual["facts"]["actions"][0]["description"] == ""


def test_a_person_and_pet_are_not_treated_as_a_single_observed_actor():
    shot = facts_shot(action="小狗从院子走向门口；主讲人双手下垂")
    shot["facts"]["subjects"].append(
        {"subject_id": "pet", "label": "宠物", "kind": "OBJECT", "visibility": "VISIBLE"}
    )
    shot["facts"]["actions"] = [
        action(
            translation="STATIONARY",
            gait="NONE",
            gesture="NONE",
            direction="in_place",
            path="站位不变",
            hand_detail="双手下垂",
            description="主讲人站在原地",
        ),
        action("pet", gesture="NONE", hand_detail="", description="小狗从院子走向门口"),
    ]
    observed = normalize_storyboard_shots([shot], preserve_observations=True)[0]
    assert observed["facts"]["actions"][0]["description"] == "主讲人站在原地；主讲人双手下垂"
    assert observed["facts"]["actions"][1]["description"] == "小狗从院子走向门口"
    assert not storyboard_facts_issues([observed])


def test_shared_static_phrase_does_not_invite_gestures():
    phrase = subject_motion_phrase(
        {"subject_motion_state": "STATIC", "subject_direction": "in_place"}
    )
    assert "静止" in phrase
    assert "手势" not in phrase
    assert "仅" not in subject_motion_phrase({"subject_motion_state": "WALKING+GESTURING_ONLY"})


def test_facts_schema_accepts_unknown_object_gait_and_optional_description():
    shot = facts_shot()
    shot["facts"]["subjects"][0]["kind"] = "OBJECT"
    shot["facts"]["actions"][0].update(gait="UNKNOWN", gesture="NOT_APPLICABLE")
    parsed = ShotFacts.model_validate(shot["facts"])
    assert parsed.actions[0].description == ""


def test_absent_or_stale_evidence_is_a_review_gap_not_a_hard_conflict():
    shot = facts_shot()
    shot["facts"]["evidence"][0]["status"] = "STALE"
    assert not storyboard_facts_issues([shot])
    assert "FACTS_EVIDENCE_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([shot])}


def test_shared_gate_checks_facts_and_ignores_stale_legacy_fields():
    from app.storyboard_consistency import storyboard_consistency_issues

    shot = facts_shot()
    assert not storyboard_consistency_issues([shot])
    shot["facts"] = {"schema_version": "broken"}
    assert "FACTS_SCHEMA_INVALID" in {i["code"] for i in storyboard_consistency_issues([shot])}


def test_legacy_multiple_people_only_checks_the_explicit_primary_subject():
    from app.storyboard_consistency import storyboard_consistency_issues

    shot = {
        "shot_id": "s1",
        "subject": "主讲人",
        "person_count": 2,
        "action": "主讲人从院子走向门口；路人站在远处",
        "motion": {"subject_motion_state": "STATIC", "subject_direction": "none"},
    }
    assert "SUBJECT_ACTION_MOTION_CONFLICT" in {
        i["code"] for i in storyboard_consistency_issues([shot])
    }
    shot["action"] = "主讲人站在原地；路人从院子走向门口"
    assert not storyboard_consistency_issues([shot])
    shot["action"] = "主讲人看到路人从院子走向门口"
    assert not storyboard_consistency_issues([shot])


def test_provider_observation_is_not_discarded_when_description_already_exists():
    shot = facts_shot(action="主讲人站在原地")
    shot["facts"]["actions"][0]["description"] = "主讲人从院子走向门口"
    normalized = normalize_storyboard_shots([shot], preserve_observations=True)[0]
    description = normalized["facts"]["actions"][0]["description"]
    assert "主讲人站在原地" in description
    assert "主讲人从院子走向门口" in description
    assert "FACTS_ACTION_CONFLICT" in codes([normalized])
    repeated = normalize_storyboard_shots([normalized], preserve_observations=True)[0]
    assert repeated["facts"]["actions"][0]["description"].count("主讲人站在原地") == 1


def test_every_visible_actor_needs_its_own_action_before_review_can_pass():
    shot = facts_shot()
    shot["facts"]["subjects"].append(
        {"subject_id": "p2", "label": "配角", "kind": "PERSON", "visibility": "VISIBLE"}
    )
    assert "FACTS_SUBJECT_ACTION_MISSING" in {
        i["code"] for i in storyboard_facts_review_gaps([shot])
    }
    assert not storyboard_facts_issues([shot])


def test_one_actor_description_is_not_evidence_for_all_movement_fields():
    shot = facts_shot()
    shot["facts"]["evidence"][0]["fields"] = [
        "subjects",
        "actions.p1.description",
        "camera",
        "entry_boundary",
        "relative_motion",
    ]
    assert "FACTS_EVIDENCE_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([shot])}


@pytest.mark.parametrize("field,value", [("gait", "WALKING"), ("gesture", "GESTURING")])
def test_nonhuman_subject_can_walk_or_gesture_without_counting_as_a_person(field, value):
    shot = facts_shot()
    shot["facts"]["subjects"][0]["kind"] = "OBJECT"
    shot["facts"]["actions"][0].update(
        gait="NOT_APPLICABLE", gesture="NOT_APPLICABLE", hand_detail=""
    )
    shot["facts"]["actions"][0][field] = value
    assert not storyboard_facts_issues([shot])
    assert normalize_storyboard_shots([shot])[0]["person_count"] == 0


def test_unknown_hand_detail_cannot_hide_an_explicit_path_contradiction():
    shot = facts_shot()
    shot["facts"]["actions"][0].update(
        translation="STATIONARY",
        gait="NONE",
        gesture="UNKNOWN",
        direction="in_place",
        hand_detail="手部被遮挡，无法判断",
    )
    assert "FACTS_ACTION_CONFLICT" in codes([shot])


def test_negating_stillness_does_not_conflict_with_observed_movement():
    shot = facts_shot()
    shot["facts"]["actions"][0]["description"] = "主讲人并非保持静止，而是走向门口"
    assert not storyboard_facts_issues([shot])


def test_subject_kind_cannot_change_between_segments_of_one_source():
    first = facts_shot()
    second = facts_shot(shot_id="s2", start_time=4, end_time=8)
    second["facts"]["entry_boundary"] = "CONTINUOUS"
    second["facts"]["subjects"][0]["kind"] = "OBJECT"
    second["facts"]["actions"][0].update(
        gait="NOT_APPLICABLE", gesture="NOT_APPLICABLE", hand_detail=""
    )
    second["facts"]["evidence"][0].update(start_time=4, end_time=8)
    assert "FACTS_SUBJECT_IDENTITY_CONFLICT" in codes([first, second])


@pytest.mark.parametrize(
    "case,expected",
    [
        ("duplicate_subject", "FACTS_SUBJECT_ID_DUPLICATE"),
        ("duplicate_action", "FACTS_ACTION_DUPLICATE"),
        ("foreign_primary", "FACTS_PRIMARY_SUBJECT_INVALID"),
        ("foreign_action", "FACTS_ACTION_SUBJECT_INVALID"),
        ("direction", "FACTS_DIRECTION_CONFLICT"),
        ("offscreen_action", "FACTS_VISIBILITY_CONFLICT"),
        ("camera", "FACTS_CAMERA_CONFLICT"),
        ("foreign_evidence_field", "FACTS_EVIDENCE_FIELD_INVALID"),
        ("evidence_after_video", "FACTS_EVIDENCE_TIME_INVALID"),
        ("reversed_evidence", "FACTS_EVIDENCE_TIME_INVALID"),
        ("entry_not_observed", "FACTS_ENTRY_EVIDENCE_INVALID"),
    ],
)
def test_subject_camera_and_evidence_conflicts_are_located(case, expected):
    shot = facts_shot()
    facts = shot["facts"]
    if case == "duplicate_subject":
        facts["subjects"].append(copy.deepcopy(facts["subjects"][0]))
    elif case == "duplicate_action":
        facts["actions"].append(copy.deepcopy(facts["actions"][0]))
    elif case == "foreign_primary":
        facts["primary_subject_id"] = "missing"
    elif case == "foreign_action":
        facts["actions"][0]["subject_id"] = "missing"
    elif case == "direction":
        facts["actions"][0]["direction"] = "in_place"
    elif case == "offscreen_action":
        facts["subjects"][0]["visibility"] = "OFFSCREEN"
    elif case == "camera":
        facts["camera"]["detail"] = "摄影机持续推近"
    elif case == "foreign_evidence_field":
        facts["evidence"][0]["fields"] = ["actions.missing.translation"]
    elif case == "evidence_after_video":
        facts["evidence"][0]["end_time"] = 5
    elif case == "reversed_evidence":
        facts["evidence"][0].update(start_time=2, end_time=1)
    elif case == "entry_not_observed":
        facts["evidence"][0]["start_time"] = 1
    matching = [i for i in storyboard_facts_issues([shot]) if i["code"] == expected]
    assert matching and all(i["shot_id"] == "s1" for i in matching)


def test_continuous_beats_and_real_cuts_keep_distinct_camera_identity():
    first = facts_shot()
    second = facts_shot(shot_id="s2", start_time=4, end_time=8)
    second["facts"]["entry_boundary"] = "CONTINUOUS"
    second["facts"]["evidence"][0].update(start_time=4, end_time=8)
    assert not storyboard_facts_issues([first, second])
    second["facts"]["camera_shot_id"] = "c2"
    assert "FACTS_CAMERA_SHOT_CONFLICT" in codes([first, second])
    second["facts"]["entry_boundary"] = "CUT"
    assert not storyboard_facts_issues([first, second])
    second["facts"]["camera_shot_id"] = "c1"
    assert "FACTS_CAMERA_SHOT_CONFLICT" in codes([first, second])


def test_cut_evidence_may_straddle_the_entry_but_action_evidence_may_not():
    first = facts_shot()
    second = facts_shot(shot_id="s2", start_time=4, end_time=8)
    second["facts"].update(entry_boundary="CUT", camera_shot_id="c2")
    evidence = second["facts"]["evidence"][0]
    evidence.update(fields=["entry_boundary"], start_time=3.8, end_time=4.2)
    assert not storyboard_facts_issues([first, second])
    evidence["fields"].append("actions.p1")
    assert "FACTS_EVIDENCE_TIME_INVALID" in codes([first, second])


def test_stale_deleted_subject_evidence_is_kept_without_blocking_save():
    shot = facts_shot(start_time=1, end_time=4)
    shot["facts"]["entry_boundary"] = "UNKNOWN"
    shot["facts"]["evidence"][0].update(
        status="STALE", fields=["actions.deleted.translation"], start_time=5, end_time=8
    )
    # 当前阶段仍从零开始；旧证据的范围与引用已经失效，仅保留历史，不作新证明。
    shot["start_time"] = 0
    assert not storyboard_facts_issues([shot])
    assert normalize_storyboard_shots([shot])[0]["facts"]["evidence"][0]["end_time"] == 8


def test_walking_in_place_does_not_require_scene_translation():
    shot = facts_shot()
    shot["facts"]["actions"][0].update(
        translation="STATIONARY", direction="in_place", path="在跑步机上原地行走"
    )
    assert not storyboard_facts_issues([shot])
    assert "原地行走" in render_storyboard_motion(shot)


def test_unknown_boundary_does_not_excuse_a_missing_start_of_timeline():
    shot = facts_shot(start_time=1)
    shot["facts"]["entry_boundary"] = "UNKNOWN"
    shot["facts"]["evidence"] = []
    assert "FACTS_TIME_DISCONTINUITY" in codes([shot])


def test_source_video_evidence_for_one_person_does_not_cover_a_second_person():
    shot = facts_shot()
    shot["facts"]["subjects"].append(
        {"subject_id": "p2", "label": "配角", "kind": "PERSON", "visibility": "VISIBLE"}
    )
    shot["facts"]["actions"].append(action("p2"))
    shot["facts"]["evidence"][0]["fields"] = [
        "subjects",
        "actions.p1",
        "camera",
        "entry_boundary",
        "relative_motion",
    ]
    assert "FACTS_EVIDENCE_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([shot])}


def test_manual_confirmation_is_not_source_video_observation():
    shot = facts_shot()
    shot["facts"]["evidence"][0]["source"] = "MANUAL"
    assert not storyboard_facts_issues([shot])
    assert "FACTS_EVIDENCE_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([shot])}


def test_a_single_frame_cannot_complete_movement_and_camera_review():
    shot = facts_shot()
    shot["facts"]["evidence"][0]["end_time"] = 0
    assert not storyboard_facts_issues([shot])
    assert "FACTS_EVIDENCE_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([shot])}


def test_subject_screenshot_is_valid_with_separate_temporal_movement_evidence():
    shot = facts_shot()
    temporal = copy.deepcopy(shot["facts"]["evidence"][0])
    temporal["fields"].remove("subjects")
    shot["facts"]["evidence"][0].update(fields=["subjects"], end_time=0)
    shot["facts"]["evidence"].append(temporal)
    assert not storyboard_facts_issues([shot])
    assert not storyboard_facts_review_gaps([shot])


@pytest.mark.parametrize(
    "observation",
    [
        "未观看视频，仅根据描述推测",
        "没有看过视频，但应该与预期一致",
        "I have not watched the video; inferred from the prompt.",
    ],
)
def test_not_watched_video_claim_cannot_cover_fact_fields(observation):
    shot = facts_shot()
    shot["facts"]["evidence"][0]["observation"] = observation
    assert not storyboard_facts_issues([shot])
    assert "FACTS_EVIDENCE_MISSING" in {i["code"] for i in storyboard_facts_review_gaps([shot])}


def test_same_label_actors_remain_distinguishable_in_rendered_instructions():
    shot = facts_shot()
    shot["facts"]["subjects"][0]["label"] = "路人"
    shot["facts"]["subjects"].append(
        {"subject_id": "p2", "label": "路人", "kind": "PERSON", "visibility": "VISIBLE"}
    )
    shot["facts"]["actions"].append(
        action(
            "p2",
            translation="STATIONARY",
            gait="NONE",
            gesture="NONE",
            direction="in_place",
            path="站位不变",
            hand_detail="双手自然下垂",
        )
    )
    text = render_storyboard_motion(shot)
    assert "路人（p1）" in text and "路人（p2）" in text
    assert "路人（p2）：场景内站位不变" in text
